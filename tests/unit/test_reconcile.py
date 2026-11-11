"""Reconciliation decision table (Stage 4B §7), reproduced with fixture records, checkouts and GitHub data.

Each scenario builds real files (run records, a checkout's git metadata) and canned GitHub responses,
observes them with ``reconcile.observe`` and checks the decision.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
from typing import Any

import pytest
from stage5_support import (
    DISPATCH,
    PROJECT_ID,
    api,
    gh_pull,
    intent,
    make_checkout,
    outcome,
    record_dir,
    remote_responses,
    write_record,
)
from support import fake_sha

from factory.github import GitHubError, Response
from factory.project import load_project
from factory.reconcile import HUMAN_ACTION, Observations, Reconciliation, State, decide, observe

R3 = "work/APP-1-r3"


def _reconcile(
    tmp_path: Path,
    *,
    responses: dict[str, Any] | None = None,
    checkout: dict[str, Any] | None = None,
    offline: bool = False,
    revision: int = 3,
) -> tuple[Reconciliation, Observations]:
    project = load_project(DISPATCH).project
    assert project is not None
    task = project.tasks["APP-1"]
    checkout_path = make_checkout(tmp_path / "checkout", **(checkout or {}))
    github = None if offline else api(responses if responses is not None else remote_responses())[0]
    obs = observe(project, task, checkout_path, tmp_path / "state", github)
    return decide(obs), obs


def _codes(result: Reconciliation) -> set[str]:
    return {f.code for f in result.findings}


# -- §7 scenario table, row by row ------------------------------------------------------------------


def test_nothing_dispatched_is_no_run(tmp_path: Path) -> None:
    result, _ = _reconcile(tmp_path)
    assert result.state is State.NO_RUN
    assert not result.blocks_dispatch
    assert all(f.result == "INFO" for f in result.findings)


def test_process_killed_during_dispatch_is_uncertain(tmp_path: Path) -> None:
    """Intent without outcome; lock free. Archon may still be running as an orphan."""
    write_record(tmp_path / "state", intent())
    (tmp_path / "state" / PROJECT_ID / "dispatch.lock").write_text("")
    result, obs = _reconcile(tmp_path)
    assert obs.lock == "free"
    assert result.state is State.UNCERTAIN
    assert {"INTERRUPTED_DISPATCH", "ARCHON_RUN_STATUS_NOT_OBSERVED"} <= _codes(result)
    assert result.blocks_dispatch
    assert "adopt or abandon" in HUMAN_ACTION[result.state]


def test_second_coordinator_gets_a_non_destructive_refusal(tmp_path: Path) -> None:
    state = tmp_path / "state"
    write_record(state, intent())
    lock = state / PROJECT_ID / "dispatch.lock"
    lock.write_text("")
    before = sorted(p.name for p in state.rglob("*"))
    fd = os.open(lock, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # No GitHub responses: nothing may be requested while another coordinator holds the lock.
        result, _ = _reconcile(tmp_path, responses={})
    finally:
        os.close(fd)
    assert result.state is State.COORDINATOR_ACTIVE
    assert _codes(result) == {"COORDINATOR_LOCK_HELD"}
    assert sorted(p.name for p in state.rglob("*")) == before


def test_multiple_hosts_are_explicitly_unsupported(tmp_path: Path) -> None:
    result, _ = _reconcile(tmp_path)
    assert "MULTI_HOST_UNSUPPORTED" in _codes(result)
    # A remote branch from another host's dispatch, with no record here, fails closed.
    result, _ = _reconcile(tmp_path / "b", responses=remote_responses(remote_branches=[R3]))
    assert result.state is State.UNTRACKED_BRANCH


def test_local_branch_with_intent_and_no_remote_is_uncertain(tmp_path: Path) -> None:
    write_record(tmp_path / "state", intent())
    result, _ = _reconcile(tmp_path, checkout={"branches": {R3: fake_sha("wip")}, "worktrees": {"a": R3}})
    assert result.state is State.UNCERTAIN
    assert "INTERRUPTED_DISPATCH" in _codes(result)


@pytest.mark.parametrize(
    "checkout",
    [{"branches": {R3: fake_sha("wip")}}, {"worktrees": {"wt": R3}}],
)
def test_local_branch_without_record_is_untracked_local_branch(tmp_path: Path, checkout: dict[str, Any]) -> None:
    result, _ = _reconcile(tmp_path, checkout=checkout)
    assert result.state is State.UNTRACKED_LOCAL_BRANCH
    assert "LOCAL_BRANCH_WITHOUT_RECORD" in _codes(result)
    assert result.blocks_dispatch


def test_remote_branch_without_record_is_untracked_branch(tmp_path: Path) -> None:
    result, _ = _reconcile(tmp_path, responses=remote_responses(remote_branches=[R3]))
    assert result.state is State.UNTRACKED_BRANCH
    assert "BRANCH_WITHOUT_RECORD" in _codes(result)


def test_pull_request_without_record_is_untracked_branch(tmp_path: Path) -> None:
    responses = remote_responses(pulls={3: [gh_pull(9, R3, merged=True)]})
    result, _ = _reconcile(tmp_path, responses=responses)
    assert result.state is State.UNTRACKED_BRANCH


def test_successful_outcome_without_pr_is_no_pr(tmp_path: Path) -> None:
    write_record(tmp_path / "state", intent())
    write_record(tmp_path / "state", outcome(exit_code=0))
    result, _ = _reconcile(tmp_path, responses=remote_responses(remote_branches=[R3]))
    assert result.state is State.NO_PR
    assert result.attempts_used == 1 and result.blocks_dispatch
    assert "not retried" in HUMAN_ACTION[State.NO_PR]


def test_no_pr_and_branch_never_pushed(tmp_path: Path) -> None:
    """archon-implement commits locally and opens no PR: the usual end of a successful run."""
    write_record(tmp_path / "state", intent())
    write_record(tmp_path / "state", outcome(exit_code=0))
    result, _ = _reconcile(tmp_path, checkout={"branches": {R3: fake_sha("done")}})
    assert result.state is State.NO_PR
    assert "BRANCH_NOT_PUSHED" in _codes(result)


def test_open_pr_from_an_older_revision_is_superseded(tmp_path: Path) -> None:
    responses = remote_responses(pulls={2: [gh_pull(7, "work/APP-1-r2", state="open")]})
    result, _ = _reconcile(tmp_path, responses=responses)
    assert result.state is State.SUPERSEDED_PR_OPEN
    assert "SUPERSEDED_PR_OPEN" in _codes(result)
    assert "never counts as evidence" in HUMAN_ACTION[result.state]


def test_older_revision_merged_is_history_only(tmp_path: Path) -> None:
    responses = remote_responses(
        remote_branches=["work/APP-1-r2"], pulls={2: [gh_pull(7, "work/APP-1-r2", merged=True)]}
    )
    result, _ = _reconcile(tmp_path, responses=responses)
    assert result.state is State.NO_RUN
    assert {h.code for h in result.history} == {"OLDER_REVISION_MERGED", "OLDER_REVISION_BRANCH_PRESENT"}
    merged = next(h for h in result.history if h.code == "OLDER_REVISION_MERGED")
    assert "not evidence or approval for revision 3" in merged.message
    assert all(h.result == "INFO" for h in result.history)


def test_older_merged_pr_never_makes_the_current_revision_merged(tmp_path: Path) -> None:
    write_record(tmp_path / "state", intent())
    write_record(tmp_path / "state", outcome())
    responses = remote_responses(pulls={1: [gh_pull(3, "work/APP-1-r1", merged=True)]})
    result, _ = _reconcile(tmp_path, responses=responses)
    assert result.state is State.NO_PR


# -- other states ---------------------------------------------------------------------------------


def test_current_pr_states(tmp_path: Path) -> None:
    for name, pulls, expected in [
        ("open", [gh_pull(9, R3)], State.PR_OPEN),
        ("merged", [gh_pull(9, R3, merged=True)], State.PR_MERGED),
        ("closed", [gh_pull(9, R3, state="closed")], State.PR_CLOSED),
    ]:
        root = tmp_path / name
        write_record(root / "state", intent())
        write_record(root / "state", outcome())
        result, _ = _reconcile(root, responses=remote_responses(remote_branches=[R3], pulls={3: pulls}))
        assert result.state is expected, name


def test_failed_run_and_exhausted_attempts(tmp_path: Path) -> None:
    write_record(tmp_path / "state", intent())
    write_record(tmp_path / "state", outcome(exit_code=2))
    result, _ = _reconcile(tmp_path)
    assert result.state is State.RUN_FAILED
    assert result.attempts_used == 1

    exhausted = tmp_path / "exhausted"
    for attempt in (1, 2):
        write_record(exhausted / "state", intent(attempt=attempt))
        write_record(exhausted / "state", outcome(attempt=attempt, exit_code=2))
    result, _ = _reconcile(exhausted)
    assert result.state is State.ATTEMPTS_EXHAUSTED
    assert (result.attempts_used, result.attempt_cap) == (2, 2)


def test_missing_checkout_makes_local_facts_unknown(tmp_path: Path) -> None:
    project = load_project(DISPATCH).project
    assert project is not None
    obs = observe(project, project.tasks["APP-1"], None, tmp_path / "state", api(remote_responses())[0])
    result = decide(obs)
    assert result.state is State.UNKNOWN
    assert "CHECKOUT_NOT_GIVEN" in _codes(result)


def test_abandoned_attempt_with_branch_left_blocks(tmp_path: Path) -> None:
    write_record(tmp_path / "state", intent())
    write_record(tmp_path / "state", outcome(status="abandoned", exit_code=None))
    result, _ = _reconcile(tmp_path, responses=remote_responses(remote_branches=[R3]))
    assert result.state is State.ABANDONED
    assert "BRANCH_PRESENT_AFTER_ABANDON" in _codes(result)
    assert result.blocks_dispatch


def test_records_for_a_newer_revision_are_uncertain(tmp_path: Path) -> None:
    write_record(tmp_path / "state", intent(revision=4))
    result, _ = _reconcile(tmp_path)
    assert result.state is State.UNCERTAIN
    assert "RECORDS_FOR_NEWER_REVISION" in _codes(result)


def test_branches_for_newer_revisions_are_uncertain(tmp_path: Path) -> None:
    result, _ = _reconcile(tmp_path, responses=remote_responses(remote_branches=["work/APP-1-r4"]))
    assert result.state is State.UNCERTAIN
    result, _ = _reconcile(tmp_path / "b", checkout={"branches": {"work/APP-1-r9": fake_sha("x")}})
    assert result.state is State.UNCERTAIN


def test_similar_branch_names_are_not_confused(tmp_path: Path) -> None:
    responses = remote_responses(remote_branches=["work/APP-1-r30", "work/APP-1-r3-old", "work/APP-1-r03"])
    result, obs = _reconcile(tmp_path, responses=responses)
    assert obs.current.remote is False
    assert result.state is State.UNCERTAIN  # r30 is newer than revision 3
    responses = remote_responses(remote_branches=["work/APP-1-r3-old", "work/APP-1-r03"])
    result, obs = _reconcile(tmp_path / "b", responses=responses)
    assert obs.current.remote is False and result.state is State.NO_RUN


def test_pr_into_the_wrong_base_is_uncertain(tmp_path: Path) -> None:
    write_record(tmp_path / "state", intent())
    write_record(tmp_path / "state", outcome())
    responses = remote_responses(remote_branches=[R3], pulls={3: [gh_pull(9, R3, base="stable")]})
    result, _ = _reconcile(tmp_path, responses=responses)
    assert result.state is State.UNCERTAIN
    assert "PR_WRONG_BASE" in _codes(result)


def test_several_active_prs_from_one_branch_are_ambiguous(tmp_path: Path) -> None:
    write_record(tmp_path / "state", intent())
    write_record(tmp_path / "state", outcome())
    pulls = {3: [gh_pull(9, R3, merged=True), gh_pull(10, R3)]}
    result, _ = _reconcile(tmp_path, responses=remote_responses(remote_branches=[R3], pulls=pulls))
    assert result.state is State.UNCERTAIN
    assert "AMBIGUOUS_PULL_REQUESTS" in _codes(result)


def test_fork_pull_requests_are_ignored(tmp_path: Path) -> None:
    fork = gh_pull(9, R3)
    fork["head"]["repo"]["full_name"] = "someone/example-app"
    result, _ = _reconcile(tmp_path, responses=remote_responses(pulls={3: [fork]}))
    assert result.state is State.NO_RUN


def test_corrupt_records_are_uncertain_never_success(tmp_path: Path) -> None:
    write_record(tmp_path / "state", intent())
    (record_dir(tmp_path / "state") / "attempt-1.outcome.json").write_text("{broken")
    responses = remote_responses(remote_branches=[R3], pulls={3: [gh_pull(9, R3, merged=True)]})
    result, _ = _reconcile(tmp_path, responses=responses)
    assert result.state is State.UNCERTAIN
    assert "RECORD_CORRUPT" in _codes(result)


# -- unknown and incomplete evidence -------------------------------------------------------------


@pytest.mark.parametrize("status", [500, 403, 404])
def test_github_failures_make_the_result_unknown(tmp_path: Path, status: int) -> None:
    responses = remote_responses()
    key = next(k for k in responses if "matching-refs" in k)
    responses[key] = Response(status, {"message": "nope"})
    result, obs = _reconcile(tmp_path, responses=responses)
    assert obs.current.remote is None
    assert result.state is State.UNKNOWN
    assert "REMOTE_BRANCHES_UNKNOWN" in _codes(result)


def test_unreadable_pull_requests_are_unknown(tmp_path: Path) -> None:
    responses = remote_responses()
    key = next(k for k in responses if "work%2FAPP-1-r2" in k or "work/APP-1-r2" in k)
    responses[key] = GitHubError("connection reset")
    result, _ = _reconcile(tmp_path, responses=responses)
    assert result.state is State.UNKNOWN
    assert "PULL_REQUESTS_UNKNOWN" in _codes(result)


def test_malformed_pull_request_listing_is_unknown(tmp_path: Path) -> None:
    responses = remote_responses()
    key = next(k for k in responses if "APP-1-r3" in k and "pulls" in k)
    responses[key] = [{"number": 1}]
    result, _ = _reconcile(tmp_path, responses=responses)
    assert result.state is State.UNKNOWN


def test_offline_reconcile_is_unknown_unless_records_decide(tmp_path: Path) -> None:
    result, _ = _reconcile(tmp_path, offline=True)
    assert result.state is State.UNKNOWN
    assert "REMOTE_NOT_READ" in _codes(result)
    write_record(tmp_path / "x" / "state", intent())
    result, _ = _reconcile(tmp_path / "x", offline=True)
    assert result.state is State.UNCERTAIN


def test_checkout_of_another_repository_is_uncertain(tmp_path: Path) -> None:
    result, _ = _reconcile(tmp_path, checkout={"origin": "https://github.com/example-org/other.git"})
    assert result.state is State.UNCERTAIN
    assert "CHECKOUT_REPOSITORY_MISMATCH" in _codes(result)


def test_checkout_without_recognizable_origin_is_unknown(tmp_path: Path) -> None:
    result, _ = _reconcile(tmp_path, checkout={"origin": None})
    assert result.state is State.UNKNOWN


def test_unreadable_state_directory_is_unknown(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / PROJECT_ID).mkdir()
    (state / PROJECT_ID / "runs").symlink_to(tmp_path)
    result, _ = _reconcile(tmp_path)
    assert result.state in (State.UNKNOWN, State.UNCERTAIN)
    assert result.state is not State.NO_RUN


def test_decisions_are_deterministic(tmp_path: Path) -> None:
    write_record(tmp_path / "state", intent())
    project = load_project(DISPATCH).project
    assert project is not None
    checkout = make_checkout(tmp_path / "checkout")
    results = [
        decide(observe(project, project.tasks["APP-1"], checkout, tmp_path / "state", api(remote_responses())[0]))
        for _ in range(2)
    ]
    assert results[0] == results[1]


def test_reconcile_makes_only_get_requests_for_known_paths(tmp_path: Path) -> None:
    project = load_project(DISPATCH).project
    assert project is not None
    github, transport = api(remote_responses())
    decide(observe(project, project.tasks["APP-1"], make_checkout(tmp_path / "c"), tmp_path / "s", github))
    assert transport.requests and all(r.startswith("repos/example-org/example-app/") for r in transport.requests)
    assert sorted(set(transport.requests)) == sorted(transport.responses)
