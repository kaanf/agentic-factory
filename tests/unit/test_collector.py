"""Collector scenarios, judged end to end by the unchanged Stage 1 planner."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from conftest import codes
from github_support import (
    BASE,
    REPO,
    FakeTransport,
    Sequence,
    check_run,
    contents,
    identity,
    job,
    listing,
    page,
    pull,
    q,
    ref,
    workflow_run,
)

from factory.collector import collect
from factory.evidence import load_snapshot
from factory.github import GitHubApi, GitHubError, Response
from factory.planning import evaluate
from factory.project import load_project

M = hashlib.sha1(b"merge").hexdigest()
H2 = hashlib.sha1(b"later head").hexdigest()
P = hashlib.sha1(b"pr head").hexdigest()
BRANCH = "feat/SVC-1-r1"
CONTRACT = "openapi: 3.1.0\ninfo:\n  title: svc\n  version: 1.2.0\n"
CLOCK = lambda: datetime(2026, 11, 11, 12, 0, tzinfo=UTC)  # noqa: E731


def _project(root: Path) -> Path:
    project = root / "svc-project"
    for directory in ("factory/tasks", "factory/profiles", "specs"):
        (project / directory).mkdir(parents=True)
    (project / "factory/project.yaml").write_text(
        f"""schemaVersion: factory.project/v1
project: {{id: svc-project}}
approvers: [example-human]
planningRepository: svc
repositories:
  - id: svc
    github: {REPO}
    branches: {{integration: dev, release: main, featurePrefix: feat/}}
    requiredChecks:
      integration:
        - {{name: "Svc Check", workflowPath: .github/workflows/ci.yml}}
    outputs:
      - {{id: svc-contract, path: api/openapi.yaml, versionPointer: /info/version}}
architecture:
  adrDirectory: docs/adr
  profiles:
    - {{id: svc-arch, repository: svc, path: factory/profiles/svc.yaml}}
dispatch: {{enabled: false}}
"""
    )
    (project / "factory/profiles/svc.yaml").write_text(
        "schemaVersion: factory.profile/v1\nid: svc-arch\nrevision: 1\nrepository: svc\nadrs: []\n"
    )
    spec = project / "specs/svc-1.md"
    spec.write_text("# Stock endpoint\n")
    digest = hashlib.sha256(spec.read_bytes()).hexdigest()
    (project / "factory/tasks/SVC-1.yaml").write_text(
        f"""schemaVersion: factory.task/v1
id: SVC-1
revision: 1
title: Stock endpoint
repository: svc
kind: implementation
specification: {{path: specs/svc-1.md, sha256: {digest}}}
architecture: {{profile: svc-arch, profileRevision: 1}}
provides: [svc-contract]
acceptance: [GET /items works]
"""
    )
    return project


@pytest.fixture
def project(tmp_path: Path) -> Path:
    return _project(tmp_path)


def commit_responses(
    sha: str,
    *,
    head: str,
    runs: list[dict[str, Any]] | None = None,
    workflows: list[dict[str, Any]] | None = None,
    jobs: dict[int, list[dict[str, Any]]] | None = None,
    pulls: list[dict[str, Any]] | None = None,
    compare: str = "identical",
) -> dict[str, Any]:
    runs = [check_run(100, sha)] if runs is None else runs
    workflows = [workflow_run(500, sha)] if workflows is None else workflows
    jobs = {500: [job(100)]} if jobs is None else jobs
    responses: dict[str, Any] = {
        page(f"{BASE}/commits/{sha}/pulls"): [pull(7, BRANCH, merged_sha=M)] if pulls is None else pulls,
        page(f"{BASE}/commits/{sha}/check-runs", filter="all"): listing("check_runs", runs),
        page(f"{BASE}/actions/runs", head_sha=sha): listing("workflow_runs", workflows),
        q(f"{BASE}/compare/{sha}...{head}"): {"status": compare},
    }
    for run_id, run_jobs in jobs.items():
        responses[page(f"{BASE}/actions/runs/{run_id}/jobs", filter="all")] = listing("jobs", run_jobs)
    return responses


def world(head: str = M, **commit: Any) -> dict[str, Any]:
    """A merged SVC-1 PR (#7, merge commit M) whose merge commit is, by default, the integration head."""
    responses: dict[str, Any] = {
        BASE: identity(),
        f"{BASE}/git/ref/heads/dev": ref(head),
        page(f"{BASE}/pulls", head=f"example-org:{BRANCH}", state="all"): [pull(7, BRANCH, merged_sha=M)],
        q(f"{BASE}/contents/api/openapi.yaml", ref=head): contents(CONTRACT),
    }
    responses.update(commit_responses(M, head=head, **commit))
    return responses


def run_collection(
    project: Path, responses: dict[str, Any], tmp_path: Path
) -> tuple[dict[str, Any], Any, FakeTransport]:
    loaded = load_project(project).project
    assert loaded is not None
    transport = FakeTransport(responses)
    result = collect(loaded, GitHubApi(transport), now=CLOCK)
    # The collector never produces approvals (Stage 3). Add simulated ones, for planning only, so
    # the levels above 'approved' reflect the collected GitHub facts rather than missing approvals.
    planned = {
        **result.snapshot,
        "approvals": [
            {"approved": True, "mergedBy": "example-human", "path": f, "sha256": loaded.digests[f]}
            for f in ("factory/tasks/SVC-1.yaml", "factory/profiles/svc.yaml")
        ],
    }
    path = tmp_path / "collected.json"
    path.write_text(json.dumps(planned))
    snapshot = load_snapshot(path, loaded)
    assert snapshot.errors == [], snapshot.errors
    assert snapshot.snapshot is not None
    plan = evaluate(loaded, snapshot.snapshot)
    task = plan.tasks[0].to_dict()
    return result.snapshot, task, transport


def level(task: dict[str, Any], name: str) -> tuple[str, set[str]]:
    return task["levels"][name]["result"], codes(task["levels"][name]["reasons"])


# -- the happy path -------------------------------------------------------------


def test_merged_pr_with_exact_sha_push_ci_is_validated(project: Path, tmp_path: Path) -> None:
    snapshot, task, transport = run_collection(project, world(), tmp_path)
    assert level(task, "merged") == ("PASS", set())
    assert level(task, "validated") == ("PASS", set())
    assert level(task, "integration-ready") == ("PASS", set())
    assert snapshot["provenance"]["kind"] == "github"
    assert snapshot["collection"] == {"collector": "factory 0.1.0", "complete": True, "errors": []}
    assert snapshot["observedAt"] == "2026-11-11T12:00:00Z"
    assert snapshot["approvals"] == []
    facts = snapshot["repositories"]["svc"]
    assert facts["integrationHead"] == M
    assert facts["commits"][M]["outputs"] == {"svc-contract": {"present": True, "version": "1.2.0"}}
    assert task["state"] == "COMPLETE"


def test_collector_only_issues_get_requests_to_repos_paths(project: Path, tmp_path: Path) -> None:
    _, _, transport = run_collection(project, world(), tmp_path)
    assert transport.requests
    assert all(r.startswith(f"repos/{REPO}") for r in transport.requests)


# -- PR head CI is not integration evidence --------------------------------------


def test_pr_head_ci_without_integration_commit_ci(project: Path, tmp_path: Path) -> None:
    pr_run = [check_run(100, M)]
    pr_workflow = [workflow_run(500, M, event="pull_request", branch=BRANCH)]
    snapshot, task, transport = run_collection(project, world(runs=pr_run, workflows=pr_workflow), tmp_path)
    assert level(task, "merged") == ("PASS", set())
    assert level(task, "validated") == ("UNKNOWN", {"CHECK_MISSING"})
    assert not any(P in r for r in transport.requests)


def test_no_ci_on_merge_commit_at_all(project: Path, tmp_path: Path) -> None:
    _, task, transport = run_collection(project, world(runs=[], workflows=[], jobs={}), tmp_path)
    assert level(task, "validated") == ("UNKNOWN", {"CHECK_MISSING"})
    # No Actions check runs means no workflow lookups are needed.
    assert not any("actions/runs" in r for r in transport.requests)


@pytest.mark.parametrize(
    ("workflow", "result", "code"),
    [
        ({"path": ".github/workflows/other.yml"}, "FAIL", "CHECK_PROVENANCE_MISMATCH"),
        ({"event": "workflow_dispatch"}, "UNKNOWN", "CHECK_MISSING"),
        ({"event": "pull_request"}, "UNKNOWN", "CHECK_MISSING"),
        ({"branch": "main"}, "UNKNOWN", "CHECK_MISSING"),
        ({"branch": "feat/SVC-1-r1"}, "UNKNOWN", "CHECK_MISSING"),
    ],
)
def test_wrong_workflow_event_or_branch(
    project: Path, tmp_path: Path, workflow: dict[str, str], result: str, code: str
) -> None:  # noqa: E501
    responses = world(workflows=[workflow_run(500, M, **workflow)])
    _, task, _ = run_collection(project, responses, tmp_path)
    assert level(task, "validated") == (result, {code})


def test_check_from_wrong_app(project: Path, tmp_path: Path) -> None:
    responses = world(runs=[check_run(100, M, app="custom-ci")], workflows=[], jobs={})
    snapshot, task, transport = run_collection(project, responses, tmp_path)
    assert level(task, "validated") == ("FAIL", {"CHECK_PROVENANCE_MISMATCH"})
    run = snapshot["repositories"]["svc"]["commits"][M]["checkRuns"][0]
    assert (run["app"], run["event"], run["workflowPath"]) == ("custom-ci", "", "")
    assert not any("actions/runs" in r for r in transport.requests)


def test_check_run_for_another_commit_is_ignored(project: Path, tmp_path: Path) -> None:
    responses = world(runs=[check_run(100, P)], workflows=[], jobs={})
    snapshot, task, _ = run_collection(project, responses, tmp_path)
    assert snapshot["repositories"]["svc"]["commits"][M]["checkRuns"] == []
    assert level(task, "validated") == ("UNKNOWN", {"CHECK_MISSING"})


def test_unrelated_check_names_do_not_count(project: Path, tmp_path: Path) -> None:
    responses = world(runs=[check_run(100, M, name="Lint")], jobs={500: [job(100)]})
    _, task, _ = run_collection(project, responses, tmp_path)
    assert level(task, "validated") == ("UNKNOWN", {"CHECK_MISSING"})


# -- conclusions ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "conclusion", "result", "code"),
    [
        ("completed", "failure", "FAIL", "CHECK_FAILED"),
        ("completed", "timed_out", "FAIL", "CHECK_FAILED"),
        ("completed", "neutral", "UNKNOWN", "CHECK_INCONCLUSIVE"),
        ("completed", "some_future_value", "UNKNOWN", "CHECK_INCONCLUSIVE"),
        ("in_progress", None, "UNKNOWN", "CHECK_IN_PROGRESS"),
        ("queued", None, "UNKNOWN", "CHECK_IN_PROGRESS"),
        ("waiting", None, "UNKNOWN", "CHECK_IN_PROGRESS"),
        ("pending", None, "UNKNOWN", "CHECK_IN_PROGRESS"),
    ],
)
def test_failed_and_pending_checks(
    project: Path, tmp_path: Path, status: str, conclusion: str | None, result: str, code: str
) -> None:  # noqa: E501
    responses = world(runs=[check_run(100, M, status=status, conclusion=conclusion)])
    _, task, _ = run_collection(project, responses, tmp_path)
    assert level(task, "validated") == (result, {code})


# -- reruns and duplicates -----------------------------------------------------------


def test_rerun_success_after_failure_passes(project: Path, tmp_path: Path) -> None:
    runs = [check_run(100, M, conclusion="failure"), check_run(101, M)]
    responses = world(runs=runs, workflows=[workflow_run(500, M, attempt=2)], jobs={500: [job(100, 1), job(101, 2)]})
    snapshot, task, _ = run_collection(project, responses, tmp_path)
    attempts = sorted(
        (r["attempt"], r["conclusion"]) for r in snapshot["repositories"]["svc"]["commits"][M]["checkRuns"]
    )
    assert attempts == [(1, "failure"), (2, "success")]
    assert level(task, "validated") == ("PASS", set())


def test_rerun_failure_after_success_fails(project: Path, tmp_path: Path) -> None:
    runs = [check_run(100, M), check_run(101, M, conclusion="failure")]
    responses = world(runs=runs, workflows=[workflow_run(500, M, attempt=2)], jobs={500: [job(100, 1), job(101, 2)]})
    _, task, _ = run_collection(project, responses, tmp_path)
    assert level(task, "validated") == ("FAIL", {"CHECK_FAILED"})


def test_duplicate_runs_with_conflicting_results_are_ambiguous(project: Path, tmp_path: Path) -> None:
    runs = [check_run(100, M, suite=1), check_run(200, M, suite=2, conclusion="failure")]
    workflows = [workflow_run(500, M, suite=1), workflow_run(600, M, suite=2)]
    responses = world(runs=runs, workflows=workflows, jobs={500: [job(100)], 600: [job(200)]})
    _, task, _ = run_collection(project, responses, tmp_path)
    assert level(task, "validated") == ("UNKNOWN", {"AMBIGUOUS_CHECK_RUN"})


def test_duplicate_successful_runs_pass(project: Path, tmp_path: Path) -> None:
    runs = [check_run(100, M, suite=1), check_run(200, M, suite=2)]
    workflows = [workflow_run(500, M, suite=1), workflow_run(600, M, suite=2)]
    responses = world(runs=runs, workflows=workflows, jobs={500: [job(100)], 600: [job(200)]})
    _, task, _ = run_collection(project, responses, tmp_path)
    assert level(task, "validated") == ("PASS", set())


# -- incomplete check data is never partially written ----------------------------------


@pytest.mark.parametrize(
    "change",
    [
        {"workflows": []},  # check suite without a findable workflow run
        {"jobs": {500: []}},  # no job (and so no attempt) for the check run
    ],
)
def test_incomplete_workflow_data_omits_all_check_runs(project: Path, tmp_path: Path, change: dict[str, Any]) -> None:
    runs = [check_run(100, M, conclusion="failure"), check_run(101, M, name="Svc Check")]
    responses = world(runs=runs, **{"jobs": {500: [job(100), job(101)]}, **change})
    snapshot, task, _ = run_collection(project, responses, tmp_path)
    assert "checkRuns" not in snapshot["repositories"]["svc"]["commits"][M]
    assert level(task, "validated") == ("UNKNOWN", {"CHECK_EVIDENCE_MISSING"})
    assert snapshot["collection"]["complete"] is False


def test_total_count_mismatch_is_incomplete(project: Path, tmp_path: Path) -> None:
    responses = world()
    responses[page(f"{BASE}/commits/{M}/check-runs", filter="all")] = {
        "check_runs": [check_run(100, M)],
        "total_count": 2,
    }
    snapshot, task, _ = run_collection(project, responses, tmp_path)
    assert level(task, "validated") == ("UNKNOWN", {"CHECK_EVIDENCE_MISSING"})
    assert "reported 2 items but returned 1" in snapshot["collection"]["errors"][0]["message"]


def test_pagination_collects_every_page(project: Path, tmp_path: Path) -> None:
    responses = world()
    others = [check_run(1000 + i, M, name=f"Other {i}", app="other-app") for i in range(100)]
    key = page(f"{BASE}/commits/{M}/check-runs", filter="all")
    del responses[key]
    responses[key] = {"check_runs": others, "total_count": 101}
    responses[q(f"{BASE}/commits/{M}/check-runs", filter="all", page=2, per_page=100)] = {
        "check_runs": [check_run(100, M)],
        "total_count": 101,
    }
    snapshot, task, _ = run_collection(project, responses, tmp_path)
    assert len(snapshot["repositories"]["svc"]["commits"][M]["checkRuns"]) == 101
    assert level(task, "validated") == ("PASS", set())


# -- PR association ----------------------------------------------------------------------


def test_commit_pulls_picks_the_pr_whose_merge_commit_matches(project: Path, tmp_path: Path) -> None:
    # The real 4B case: one SHA listed by an open release PR and by the PR that merged it.
    pulls = [pull(3, "dev", base="main", state="open"), pull(7, BRANCH, merged_sha=M)]
    snapshot, task, _ = run_collection(project, world(pulls=pulls), tmp_path)
    assert snapshot["repositories"]["svc"]["commits"][M]["mergedPullRequests"] == [7]
    assert level(task, "validated") == ("PASS", set())


def test_two_merged_prs_claiming_one_commit_are_ambiguous(project: Path, tmp_path: Path) -> None:
    pulls = [pull(7, BRANCH, merged_sha=M), pull(8, "feat/other", merged_sha=M)]
    _, task, _ = run_collection(project, world(pulls=pulls), tmp_path)
    assert level(task, "validated") == ("UNKNOWN", {"AMBIGUOUS_PR_MAPPING"})


def test_open_pr_test_merge_sha_is_not_recorded(project: Path, tmp_path: Path) -> None:
    responses = world()
    responses[page(f"{BASE}/pulls", head=f"example-org:{BRANCH}", state="all")] = [pull(7, BRANCH, state="open")]
    snapshot, task, _ = run_collection(project, responses, tmp_path)
    assert snapshot["repositories"]["svc"]["branches"][BRANCH]["pullRequests"] == [
        {"baseBranch": "dev", "mergeCommitSha": None, "number": 7, "state": "open"}
    ]
    # The PR body claims success; it is never read and changes nothing.
    assert level(task, "merged") == ("FAIL", {"NOT_MERGED"})
    assert task["state"] == "IN_REVIEW"


def test_fork_pr_with_same_branch_name_is_ignored(project: Path, tmp_path: Path) -> None:
    responses = world()
    responses[page(f"{BASE}/pulls", head=f"example-org:{BRANCH}", state="all")] = [
        pull(9, BRANCH, merged_sha=M, head_repo="someone/example-svc")
    ]
    responses = {k: v for k, v in responses.items() if f"/commits/{M}" not in k and f"/compare/{M}" not in k}
    responses.update(commit_responses(M, head=M))
    snapshot, task, _ = run_collection(project, responses, tmp_path)
    assert snapshot["repositories"]["svc"]["branches"][BRANCH]["pullRequests"] == []
    assert level(task, "merged") == ("FAIL", {"NOT_MERGED"})


def test_pr_merged_into_wrong_base(project: Path, tmp_path: Path) -> None:
    responses = world()
    responses[page(f"{BASE}/pulls", head=f"example-org:{BRANCH}", state="all")] = [
        pull(7, BRANCH, base="main", merged_sha=M)
    ]
    _, task, _ = run_collection(project, responses, tmp_path)
    assert level(task, "merged") == ("FAIL", {"PR_MERGED_INTO_WRONG_BASE"})


# -- ancestry and head movement --------------------------------------------------------------


def test_unreachable_merge_commit(project: Path, tmp_path: Path) -> None:
    responses = world(head=H2, compare="diverged")
    responses.update(
        commit_responses(
            H2,
            head=H2,
            runs=[check_run(300, H2)],
            workflows=[workflow_run(700, H2)],
            jobs={700: [job(300)]},
            pulls=[pull(8, "feat/other", merged_sha=H2)],
        )
    )
    responses[page(f"{BASE}/commits/{M}/pulls")] = [pull(7, BRANCH, merged_sha=M)]
    _, task, _ = run_collection(project, responses, tmp_path)
    assert level(task, "validated") == ("FAIL", {"COMMIT_NOT_REACHABLE"})


def test_validated_merge_but_unvalidated_new_head(project: Path, tmp_path: Path) -> None:
    responses = world(head=H2, compare="ahead")
    responses.update(
        commit_responses(
            H2,
            head=H2,
            runs=[check_run(300, H2, status="in_progress", conclusion=None)],
            workflows=[workflow_run(700, H2)],
            jobs={700: [job(300)]},
            pulls=[pull(8, "feat/other", merged_sha=H2)],
        )
    )
    responses[page(f"{BASE}/commits/{M}/pulls")] = [pull(7, BRANCH, merged_sha=M)]
    _, task, _ = run_collection(project, responses, tmp_path)
    assert level(task, "validated") == ("PASS", set())
    assert level(task, "integration-ready") == ("UNKNOWN", {"HEAD_NOT_VALIDATED", "CHECK_IN_PROGRESS"})


def test_integration_head_moving_during_collection(project: Path, tmp_path: Path) -> None:
    responses = world()
    responses[f"{BASE}/git/ref/heads/dev"] = Sequence([ref(M), ref(H2)])
    snapshot, task, _ = run_collection(project, responses, tmp_path)
    facts = snapshot["repositories"]["svc"]
    assert "integrationHead" not in facts
    assert "reachableFromIntegrationHead" not in facts["commits"][M]
    assert snapshot["collection"]["errors"] == [
        {"fact": "integrationHead", "message": f"moved from {M} to {H2} during collection", "repository": "svc"}
    ]
    assert level(task, "validated") == ("UNKNOWN", {"REACHABILITY_UNKNOWN"})
    assert level(task, "integration-ready")[0] == "UNKNOWN"


# -- API failures and rate limits --------------------------------------------------------------


def test_server_error_omits_only_the_affected_fact(project: Path, tmp_path: Path) -> None:
    responses = world()
    responses[page(f"{BASE}/commits/{M}/check-runs", filter="all")] = Response(502, {"message": "Bad Gateway"})
    snapshot, task, _ = run_collection(project, responses, tmp_path)
    commit = snapshot["repositories"]["svc"]["commits"][M]
    assert "checkRuns" not in commit and commit["mergedPullRequests"] == [7]
    assert level(task, "merged") == ("PASS", set())
    assert level(task, "validated") == ("UNKNOWN", {"CHECK_EVIDENCE_MISSING"})
    assert snapshot["collection"]["errors"][0]["message"] == "HTTP 502 Bad Gateway"


def test_transport_failure_is_unknown(project: Path, tmp_path: Path) -> None:
    responses = world()
    responses[q(f"{BASE}/compare/{M}...{M}")] = GitHubError("gh api failed: connection reset")
    _, task, _ = run_collection(project, responses, tmp_path)
    assert level(task, "validated") == ("UNKNOWN", {"REACHABILITY_UNKNOWN"})


def test_inaccessible_repository_yields_no_facts(project: Path, tmp_path: Path) -> None:
    snapshot, task, transport = run_collection(project, {BASE: Response(404, {"message": "Not Found"})}, tmp_path)
    assert snapshot["repositories"] == {}
    assert transport.requests == [BASE]
    assert level(task, "merged") == ("UNKNOWN", {"PR_EVIDENCE_MISSING"})


def test_renamed_repository_is_not_trusted(project: Path, tmp_path: Path) -> None:
    snapshot, _, transport = run_collection(project, {BASE: identity("example-org/renamed-svc")}, tmp_path)
    assert snapshot["repositories"] == {}
    assert "renamed or transferred" in snapshot["collection"]["errors"][0]["message"]


@pytest.mark.parametrize(
    "limited",
    [
        Response(403, {"message": "API rate limit exceeded"}, {"x-ratelimit-remaining": "0"}),
        Response(429, {"message": "Too Many Requests"}),
        Response(403, {"message": "You have exceeded a secondary rate limit."}),
    ],
)
def test_rate_limit_stops_collection_and_nothing_passes(project: Path, tmp_path: Path, limited: Response) -> None:
    responses = world()
    responses[page(f"{BASE}/pulls", head=f"example-org:{BRANCH}", state="all")] = limited
    snapshot, task, transport = run_collection(project, responses, tmp_path)
    # Once limited, no further request is sent.
    assert transport.requests[-1].startswith(f"{BASE}/pulls")
    assert snapshot["collection"]["complete"] is False
    for name in ("merged", "validated", "integration-ready"):
        assert task["levels"][name]["result"] != "PASS"
    assert any("rate limit" in e["message"] for e in snapshot["collection"]["errors"])


def test_forbidden_without_rate_limit_is_a_plain_error(project: Path, tmp_path: Path) -> None:
    responses = world()
    responses[page(f"{BASE}/pulls", head=f"example-org:{BRANCH}", state="all")] = Response(
        403, {"message": "Resource not accessible by integration"}, {"x-ratelimit-remaining": "4000"}
    )
    snapshot, task, transport = run_collection(project, responses, tmp_path)
    assert level(task, "merged") == ("UNKNOWN", {"PR_EVIDENCE_MISSING"})
    assert len(transport.requests) > 3  # collection continued for other facts


# -- declared outputs --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("response", "expected", "result"),
    [
        (Response(404, {"message": "Not Found"}), {"present": False, "version": None}, "FAIL"),
        (contents("info: {title: svc}\n"), {"present": True, "version": None}, "UNKNOWN"),
        (contents("info: {version: 1.2}\n"), {"present": True, "version": None}, "UNKNOWN"),
        (contents("{not: [valid"), {"present": True, "version": None}, "UNKNOWN"),
        (contents('{"info": {"version": "2.0.0"}}'), {"present": True, "version": "2.0.0"}, "PASS"),
    ],
)
def test_output_contents_at_head(
    project: Path, tmp_path: Path, response: Any, expected: dict[str, Any], result: str
) -> None:  # noqa: E501
    responses = world()
    responses[q(f"{BASE}/contents/api/openapi.yaml", ref=M)] = response
    snapshot, task, _ = run_collection(project, responses, tmp_path)
    assert snapshot["repositories"]["svc"]["commits"][M]["outputs"]["svc-contract"] == expected
    assert level(task, "integration-ready")[0] == result


def test_output_that_is_not_a_file_is_omitted(project: Path, tmp_path: Path) -> None:
    responses = world()
    responses[q(f"{BASE}/contents/api/openapi.yaml", ref=M)] = [{"type": "file", "name": "a.yaml"}]
    snapshot, task, _ = run_collection(project, responses, tmp_path)
    assert snapshot["repositories"]["svc"]["commits"][M]["outputs"] == {}
    assert level(task, "integration-ready") == ("UNKNOWN", {"OUTPUT_EVIDENCE_MISSING"})
