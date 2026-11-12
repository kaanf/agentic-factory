"""Regression tests for the Stage 5 security review findings F1-F6, through the production call chains.

Real files everywhere (checkouts in git's on-disk layout, Archon homes, run records, lock files); the
only stand-in is the GitHub transport, which serves canned API responses. Each test states the
invariant it protects: unknown stays UNKNOWN, uncertain stays UNCERTAIN, omitted data never becomes
NO_RUN or PASS, and authorization is never granted.
"""

from __future__ import annotations

import dataclasses
import fcntl
import hashlib
import io
import json
import os
import subprocess
import tracemalloc
from pathlib import Path
from typing import Any

import pytest
from github_support import FakeTransport, page, q
from stage5_support import (
    BASE,
    DISPATCH,
    PROJECT_ID,
    api,
    gh_pull,
    gh_ref,
    make_archon_home,
    make_checkout,
    preflight_responses,
    ready_snapshot,
    remote_responses,
)
from support import copy_project, fake_sha, write_snapshot

from factory import collector
from factory.cli import run
from factory.evidence import parse_snapshot
from factory.github import GitHubApi, GitHubError, Response
from factory.gitstate import read_checkout
from factory.planning import evaluate
from factory.preflight import preflight
from factory.project import load_project
from factory.reconcile import MAX_HISTORY, State, decide, observe, read_remote

R3 = "work/APP-1-r3"
PULLS_R3 = page(f"{BASE}/pulls", head=f"example-org:{R3}", state="all")
REFS = f"{BASE}/git/matching-refs/heads/work/APP-1-r"


@dataclasses.dataclass
class World:
    root: Path
    project_dir: Path
    checkout: Path
    archon: Path
    state: Path

    def project(self):  # type: ignore[no-untyped-def]
        project = load_project(self.project_dir).project
        assert project is not None
        return project

    def plan(self):  # type: ignore[no-untyped-def]
        project = self.project()
        snapshot = parse_snapshot(ready_snapshot(self.project_dir), project, "s").snapshot
        assert snapshot is not None
        return evaluate(project, snapshot)

    def preflight(self, responses: dict[str, Any] | None = None, task: str = "APP-1", revision: int = 3):  # type: ignore[no-untyped-def]
        github, transport = api(responses if responses is not None else preflight_responses(task, revision))
        report = preflight(self.project(), task, revision, self.checkout, self.state, self.archon, self.plan(), github)
        return report, transport

    def reconcile(self, responses: dict[str, Any] | None = None):  # type: ignore[no-untyped-def]
        project = self.project()
        github, _ = api(responses if responses is not None else remote_responses())
        return decide(observe(project, project.tasks["APP-1"], self.checkout, self.state, github))


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(
        root=tmp_path,
        project_dir=copy_project(DISPATCH, tmp_path),
        checkout=make_checkout(tmp_path / "checkout"),
        archon=make_archon_home(tmp_path / "archon"),
        state=tmp_path / "state",
    )


def _codes(report) -> set[str]:  # type: ignore[no-untyped-def]
    return set(report.to_dict()["dryRun"]["reasonCodes"])


def _assert_not_clear(report, reconciliation) -> None:  # type: ignore[no-untyped-def]
    """The invariant behind every finding: never PASS, never NO_RUN, never authorized."""
    assert report.result.value != "PASS"
    assert reconciliation.state is not State.NO_RUN
    assert reconciliation.blocks_dispatch
    assert report.authorization.granted is False


def _ref(checkout: Path, name: str) -> Path:
    path = checkout / ".git" / "refs" / "heads" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


# -- F1: local git state ------------------------------------------------------------------------


def test_f1_symbolic_ref_at_the_feature_branch_is_present_and_unresolved(world: World) -> None:
    _ref(world.checkout, R3).write_text("ref: refs/heads/next\n")
    state = read_checkout(world.checkout)
    assert state.branch_state(R3) == "present" and R3 in state.local.unresolved
    report, _ = world.preflight()
    reconciliation = world.reconcile()
    _assert_not_clear(report, reconciliation)
    assert {"FEATURE_BRANCH_EXISTS_LOCAL", "LOCAL_REF_UNRESOLVED", "RECONCILE_UNCERTAIN"} <= _codes(report)
    assert reconciliation.state is State.UNCERTAIN


@pytest.mark.parametrize("value", ["not-a-sha\n", "", fake_sha("x")[:39] + "\n", fake_sha("x") + "\nextra\n"])
def test_f1_malformed_ref_values_are_uncertain(world: World, value: str) -> None:
    _ref(world.checkout, R3).write_text(value)
    report, _ = world.preflight()
    reconciliation = world.reconcile()
    _assert_not_clear(report, reconciliation)
    assert reconciliation.state is State.UNCERTAIN
    assert "LOCAL_REF_UNRESOLVED" in {f.code for f in reconciliation.findings}


def test_f1_symlinked_ref_is_present_and_never_followed(world: World) -> None:
    target = world.root / "elsewhere"
    target.write_text(fake_sha("elsewhere") + "\n")
    _ref(world.checkout, R3).symlink_to(target)
    report, _ = world.preflight()
    reconciliation = world.reconcile()
    _assert_not_clear(report, reconciliation)
    assert "FEATURE_BRANCH_EXISTS_LOCAL" in _codes(report)


def test_f1_symlinked_ref_directory_makes_names_below_it_unknown(world: World) -> None:
    other = world.root / "other-refs"
    other.mkdir()
    (other / "APP-1-r3").write_text(fake_sha("hidden") + "\n")
    (world.checkout / ".git" / "refs" / "heads" / "work").symlink_to(other)
    state = read_checkout(world.checkout)
    assert state.branch_state(R3) == "unknown"  # never followed, never assumed absent
    assert state.branch_state("work") == "present"
    assert state.branch_state("unrelated") == "absent"
    report, _ = world.preflight()
    reconciliation = world.reconcile()
    _assert_not_clear(report, reconciliation)
    assert "FEATURE_BRANCH_LOCAL_UNKNOWN" in _codes(report)
    assert reconciliation.state is State.UNKNOWN


def test_f1_unreadable_ref_subdirectory_is_unknown(world: World) -> None:
    directory = world.checkout / ".git" / "refs" / "heads" / "work"
    directory.mkdir(parents=True)
    os.mkfifo(directory / "fifo-ref")
    os.chmod(directory, 0)
    try:
        if os.access(directory, os.R_OK):
            pytest.skip("running with privileges that ignore directory permissions")
        state = read_checkout(world.checkout)
        assert state.branch_state(R3) == "unknown"
        report, _ = world.preflight()
        reconciliation = world.reconcile()
        _assert_not_clear(report, reconciliation)
        assert "FEATURE_BRANCH_LOCAL_UNKNOWN" in _codes(report)
        assert reconciliation.state is State.UNKNOWN
    finally:
        os.chmod(directory, 0o755)


@pytest.mark.parametrize("head", ["directory", "garbage\n", "ref: refs/heads/bad..name\n"])
def test_f1_unreadable_linked_worktree_head_is_unknown(world: World, head: str) -> None:
    entry = world.checkout / ".git" / "worktrees" / "w1"
    entry.mkdir(parents=True)
    if head == "directory":
        (entry / "HEAD").mkdir()
    else:
        (entry / "HEAD").write_text(head)
    assert read_checkout(world.checkout).worktree_state(R3) == "unknown"
    report, _ = world.preflight()
    reconciliation = world.reconcile()
    _assert_not_clear(report, reconciliation)
    assert "FEATURE_WORKTREE_UNKNOWN" in _codes(report)
    assert reconciliation.state is State.UNKNOWN


def test_f1_worktree_entry_that_is_not_a_directory_is_unknown(world: World) -> None:
    (world.checkout / ".git" / "worktrees").mkdir()
    (world.checkout / ".git" / "worktrees" / "w1").write_text("x")
    assert read_checkout(world.checkout).worktree_state(R3) == "unknown"
    _assert_not_clear(world.preflight()[0], world.reconcile())


def test_f1_detached_worktree_is_a_known_state(world: World) -> None:
    entry = world.checkout / ".git" / "worktrees" / "w1"
    entry.mkdir(parents=True)
    (entry / "HEAD").write_text(fake_sha("detached") + "\n")
    assert read_checkout(world.checkout).worktree_state(R3) == "absent"
    assert world.reconcile().state is State.NO_RUN


def test_f1_unknown_primary_head_is_unknown(world: World) -> None:
    (world.checkout / ".git" / "HEAD").write_text("nonsense\n")
    report, _ = world.preflight()
    assert "CHECKOUT_HEAD_UNKNOWN" in _codes(report)
    assert report.result.value != "PASS"


def test_f1_symbolic_ref_pointing_at_the_task_branch_is_uncertain(world: World) -> None:
    _ref(world.checkout, "alias").write_text(f"ref: refs/heads/{R3}\n")
    reconciliation = world.reconcile()
    assert reconciliation.state is State.UNCERTAIN
    assert "SYMBOLIC_REF_TO_TASK_BRANCH" in {f.code for f in reconciliation.findings}


def test_f1_remote_tracking_symbolic_ref_is_not_ignored(world: World) -> None:
    path = world.checkout / ".git" / "refs" / "remotes" / "origin" / "work" / "APP-1-r3"
    path.parent.mkdir(parents=True)
    path.write_text("ref: refs/remotes/origin/next\n")
    report, _ = world.preflight()
    reconciliation = world.reconcile()
    assert "REMOTE_TRACKING_BRANCH_EXISTS" in _codes(report)
    assert reconciliation.state is State.UNCERTAIN


def test_f1_origin_head_symref_is_normal(world: World) -> None:
    path = world.checkout / ".git" / "refs" / "remotes" / "origin" / "HEAD"
    path.parent.mkdir(parents=True)
    path.write_text("ref: refs/remotes/origin/next\n")
    assert world.reconcile().state is State.NO_RUN


def test_f1_packed_valid_branch_is_present(world: World) -> None:
    world.checkout = make_checkout(world.root / "packed", packed={R3: fake_sha("packed")})
    report, _ = world.preflight()
    reconciliation = world.reconcile()
    assert "FEATURE_BRANCH_EXISTS_LOCAL" in _codes(report)
    assert reconciliation.state is State.UNTRACKED_LOCAL_BRANCH


def test_f1_loose_unresolved_ref_shadows_a_valid_packed_one(world: World) -> None:
    world.checkout = make_checkout(world.root / "packed", packed={R3: fake_sha("packed")})
    _ref(world.checkout, R3).write_text("garbage\n")
    state = read_checkout(world.checkout)
    assert R3 not in state.branches and R3 in state.local.unresolved
    assert world.reconcile().state is State.UNCERTAIN


def test_f1_loose_valid_branch_is_present(world: World) -> None:
    _ref(world.checkout, R3).write_text(fake_sha("loose") + "\n")
    assert read_checkout(world.checkout).branch_state(R3) == "present"
    assert world.reconcile().state is State.UNTRACKED_LOCAL_BRANCH


def test_f1_confirmed_absence_still_reconciles_clean(world: World) -> None:
    state = read_checkout(world.checkout)
    assert state.branch_state(R3) == "absent"
    assert state.worktree_state(R3) == "absent"
    assert state.remote_tracking_state(R3) == "absent"
    assert world.reconcile().state is State.NO_RUN
    report, _ = world.preflight()
    assert not {c for c in _codes(report) if "BRANCH" in c or "WORKTREE" in c}


def test_f1_unrelated_broken_refs_stay_notes(world: World) -> None:
    _ref(world.checkout, "someone/else").write_text("garbage\n")
    reconciliation = world.reconcile()
    assert reconciliation.state is State.NO_RUN
    assert "CHECKOUT_NOTE" in {f.code for f in reconciliation.findings}


# -- F2: incomplete GitHub observations --------------------------------------------------------


@pytest.mark.parametrize(
    "refs",
    [
        [{"object": {"sha": fake_sha("x"), "type": "commit"}}],
        [{"ref": None, "object": {"sha": fake_sha("x")}}],
        [{"ref": f"refs/heads/{R3}"}],
        [{"ref": f"refs/heads/{R3}", "object": {"sha": "short"}}],
        ["refs/heads/work/APP-1-r3"],
        [gh_ref("work/APP-1-r1"), {"object": {"sha": fake_sha("y")}}],
        [gh_ref("other/branch")],
        {"ref": f"refs/heads/{R3}"},
        [gh_ref(f"work/APP-1-r{n}") for n in range(1, 101)],
    ],
    ids=[
        "missing-ref",
        "null-ref",
        "missing-object",
        "bad-sha",
        "string-entry",
        "mixed",
        "outside-prefix",
        "not-list",
        "too-many",
    ],
)
def test_f2_malformed_matching_refs_are_never_an_empty_listing(world: World, refs: Any) -> None:
    responses = preflight_responses()
    responses[REFS] = refs
    responses[f"{BASE}/git/matching-refs/heads/{R3}"] = refs
    report, _ = world.preflight(responses)
    reconciliation = world.reconcile({**remote_responses(), REFS: refs})
    _assert_not_clear(report, reconciliation)
    assert reconciliation.state in (State.UNKNOWN, State.UNCERTAIN)
    assert {"REMOTE_BRANCH_UNKNOWN", "RECONCILE_UNKNOWN"} & _codes(report)


def _with_pr(record: dict[str, Any]) -> dict[str, Any]:
    responses = remote_responses()
    responses[PULLS_R3] = [record]
    return responses


@pytest.mark.parametrize(
    "change",
    [
        lambda pr: pr["head"].pop("repo"),
        lambda pr: pr["head"].update(repo=None),
        lambda pr: pr["head"].update(repo={}),
        lambda pr: pr["head"].update(repo={"full_name": None}),
        lambda pr: pr["head"].pop("ref"),
        lambda pr: pr.update(head=None),
        lambda pr: pr.pop("number"),
        lambda pr: pr.update(number="9"),
        lambda pr: pr.update(number=True),
        lambda pr: pr.pop("state"),
        lambda pr: pr.update(state="weird"),
        lambda pr: pr.pop("merged_at"),
        lambda pr: pr.update(base=None),
        lambda pr: pr.update(state="open", merged_at="2026-11-11T07:00:00Z"),
    ],
    ids=[
        "missing-head-repo", "null-head-repo", "empty-head-repo", "null-full-name", "missing-head-ref", "null-head",
        "missing-number", "string-number", "bool-number", "missing-state", "bad-state", "missing-merged-at",
        "null-base", "open-but-merged",
    ],
)  # fmt: skip
def test_f2_pull_requests_with_unknown_identity_are_incomplete(world: World, change) -> None:  # type: ignore[no-untyped-def]
    record = gh_pull(9, R3)
    change(record)
    responses = _with_pr(record)
    reconciliation = world.reconcile(responses)
    assert reconciliation.state is State.UNKNOWN
    assert "PULL_REQUESTS_UNKNOWN" in {f.code for f in reconciliation.findings}
    preflight_with = {**preflight_responses(), PULLS_R3: [record]}
    report, _ = world.preflight(preflight_with)
    _assert_not_clear(report, reconciliation)


def test_f2_mixed_valid_and_malformed_records_are_incomplete(world: World) -> None:
    bad = gh_pull(10, R3)
    bad["head"]["repo"] = None
    responses = _with_pr(gh_pull(9, R3, state="closed"))
    responses[PULLS_R3] = [gh_pull(9, R3, state="closed"), bad]
    assert world.reconcile(responses).state is State.UNKNOWN


def test_f2_confirmed_foreign_repository_pr_is_ignored(world: World) -> None:
    fork = gh_pull(9, R3)
    fork["head"]["repo"] = {"full_name": "someone/example-app"}
    assert world.reconcile(_with_pr(fork)).state is State.NO_RUN


def test_f2_confirmed_other_branch_pr_is_ignored_even_without_repo(world: World) -> None:
    other = gh_pull(9, "work/APP-1-r3-old")
    other["head"]["repo"] = None
    assert world.reconcile(_with_pr(other)).state is State.NO_RUN


def test_f2_pagination_failure_is_blocking(world: World) -> None:
    responses = remote_responses()
    full = [gh_pull(n, "work/elsewhere") for n in range(1, 101)]
    responses[PULLS_R3] = full
    responses[q(f"{BASE}/pulls", head=f"example-org:{R3}", page=2, per_page=100, state="all")] = Response(
        502, {"message": "Bad Gateway"}
    )
    reconciliation = world.reconcile(responses)
    assert reconciliation.state is State.UNKNOWN


def test_f2_the_stage2_collector_omits_a_branch_with_incomplete_records() -> None:
    """The shared listing is also the Stage 2 collector's: the fact is left out (UNKNOWN), not filtered."""
    project = load_project(DISPATCH).project
    assert project is not None
    bad = gh_pull(9, R3)
    bad["head"]["repo"] = None
    transport = FakeTransport({PULLS_R3: [bad]})
    with pytest.raises(GitHubError):
        collector.branch_pull_requests(GitHubApi(transport), project.manifest.repository("app"), R3)


# -- F4: effective Archon provider ---------------------------------------------------------------


def _with_repo_config(world: World, text: str, remote: str | None = "worktree:\n  baseBranch: next\n"):  # type: ignore[no-untyped-def]
    (world.checkout / ".archon" / "config.yaml").write_text(text)
    return world.preflight(preflight_responses(remote_config=remote))[0]


REPO_BASE = "worktree:\n  baseBranch: next\n"


@pytest.mark.parametrize(
    ("global_assistant", "repo_extra", "task", "expected"),
    [
        ("claude", "assistant: codex\n", "APP-1", {"PROVIDER_OVERRIDDEN_BY_REPOSITORY"}),
        ("codex", "assistant: claude\n", "APP-2", {"PROVIDER_OVERRIDDEN_BY_REPOSITORY"}),
        ("claude", "assistant: claude\n", "APP-1", set()),
        ("codex", "assistant: claude\n", "APP-1", set()),  # the repository override is what Archon uses
        ("claude", "", "APP-2", {"PROVIDER_NOT_ARCHON_DEFAULT"}),
        (
            "claude",
            "tiers:\n  large: {provider: codex, model: gpt-6.1-sol}\n",
            "APP-1",
            {"MODEL_BINDING_PROVIDER_MISMATCH"},
        ),
        ("claude", "tiers:\n  small: {provider: codex, model: x}\n", "APP-1", {"MODEL_BINDING_PROVIDER_MISMATCH"}),
        (
            "claude",
            "aliases:\n  '@implementer': {provider: codex, model: x}\n",
            "APP-1",
            {"MODEL_BINDING_PROVIDER_MISMATCH"},
        ),
        (
            "claude",
            "aliases:\n  '@implementer': {provider: gemini, model: x}\n",
            "APP-1",
            {"MODEL_BINDING_PROVIDER_MISMATCH"},
        ),
        ("claude", "aliases:\n  '@implementer': codex/gpt-6.1-sol\n", "APP-1", {"MODEL_BINDING_UNSUPPORTED"}),
        ("claude", "aliases:\n  '@implementer': '@other'\n", "APP-1", {"MODEL_BINDING_UNSUPPORTED"}),
        ("claude", "tiers:\n  huge: {provider: claude, model: x}\n", "APP-1", {"MODEL_BINDING_INVALID"}),
        ("claude", "aliases: [1, 2]\n", "APP-1", {"MODEL_BINDING_INVALID"}),
        ("claude", "assistant: --codex\n", "APP-1", {"ARCHON_ASSISTANT_INVALID"}),
        ("claude", "tiers:\n  large: {provider: claude, model: opus}\n", "APP-1", set()),
    ],
)
def test_f4_effective_provider(  # type: ignore[no-untyped-def]
    world: World, global_assistant: str, repo_extra: str, task: str, expected: set[str]
) -> None:
    make_archon_home(world.archon, assistant=global_assistant)
    (world.checkout / ".archon" / "config.yaml").write_text(REPO_BASE + repo_extra)
    revision = 3 if task == "APP-1" else 1
    report, _ = world.preflight(
        preflight_responses(task, revision, remote_config=REPO_BASE + repo_extra), task, revision
    )
    codes = _codes(report)
    provider_codes = {
        c for c in codes if c.startswith(("PROVIDER_", "MODEL_BINDING_", "ARCHON_ASSISTANT", "ARCHON_REPO_CONFIG_C"))
    }
    assert provider_codes == expected
    # The provider is never fully established: the database layers stay unobserved.
    assert "ARCHON_DATABASE_AI_CONFIGURATION_UNOBSERVED" in codes
    assert report.result.value != "PASS"
    assert report.authorization.granted is False


def test_f4_global_bindings_are_checked_too(world: World) -> None:
    (world.archon / "config.yaml").write_text(
        "defaultAssistant: claude\naliases:\n  '@implementer': {provider: codex, model: x}\n"
    )
    report, _ = world.preflight()
    assert "MODEL_BINDING_PROVIDER_MISMATCH" in _codes(report)


def test_f4_conflicting_local_and_remote_repository_assistants(world: World) -> None:
    report = _with_repo_config(world, REPO_BASE + "assistant: claude\n", remote=REPO_BASE + "assistant: codex\n")
    assert {"ARCHON_REPO_CONFIG_CONFLICT", "PROVIDER_OVERRIDDEN_BY_REPOSITORY"} <= _codes(report)


def test_f4_missing_provider_everywhere(world: World) -> None:
    (world.archon / "config.yaml").unlink()
    report, _ = world.preflight()
    assert "ARCHON_DEFAULT_ASSISTANT_UNKNOWN" in _codes(report)


def test_f4_malformed_configuration(world: World) -> None:
    (world.archon / "config.yaml").write_text("defaultAssistant: [claude\n")
    report = _with_repo_config(world, "worktree: {baseBranch: next\n")
    assert {"ARCHON_DEFAULT_ASSISTANT_UNKNOWN", "ARCHON_REPO_CONFIG_INVALID"} <= _codes(report)


@pytest.mark.parametrize(
    ("where", "name", "content", "code"),
    [
        ("checkout", "archon-implement.yaml", "name: something-else\n", "WORKFLOW_OVERRIDE_PRESENT"),
        ("checkout", "mine.yaml", "name: archon-implement\nmodel: [codex-large]\n", "WORKFLOW_OVERRIDE_PRESENT"),
        ("checkout", "nested/deeper/x.yml", "name: archon-implement\n", "WORKFLOW_OVERRIDE_PRESENT"),
        ("checkout", "broken.yaml", "name: [\n", "ARCHON_WORKFLOW_DEFINITIONS_UNREADABLE"),
        ("home", "workflows/x.yaml", "name: archon-implement\n", "WORKFLOW_OVERRIDE_PRESENT"),
        ("home", "plugins/pack/wf/archon-implement.yaml", "name: archon-implement\n", "WORKFLOW_OVERRIDE_PRESENT"),
    ],
)
def test_f4_workflow_definitions_that_could_replace_the_reviewed_one(  # type: ignore[no-untyped-def]
    world: World, where: str, name: str, content: str, code: str
) -> None:
    base = world.checkout / ".archon" / "workflows" if where == "checkout" else world.archon
    path = base / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    report, _ = world.preflight()
    assert code in _codes(report)
    assert report.result.value != "PASS"


def test_f4_unrelated_repository_workflows_are_allowed(world: World) -> None:
    path = world.checkout / ".archon" / "workflows" / "lint.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("name: project-lint\nnodes: []\n")
    report, _ = world.preflight()
    assert not {c for c in _codes(report) if "WORKFLOW" in c}


# -- F5: bounded revision history ---------------------------------------------------------------


@pytest.mark.parametrize("revision", [1, 3, 50, 51, 1000, 1_000_000])
def test_f5_history_is_bounded_by_the_window(world: World, revision: int) -> None:
    project = world.project()
    task = dataclasses.replace(project.tasks["APP-1"], revision=revision)
    requests: list[str] = []

    class Transport:
        def get(self, path: str) -> Response:
            requests.append(path)
            return Response(200, [])

    tracemalloc.start()
    remote = read_remote(GitHubApi(Transport()), project.manifest.repository("app"), task)
    observations = observe(project, task, world.checkout, world.state, GitHubApi(Transport()))
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    window = min(revision, MAX_HISTORY + 1)
    assert remote.branches == frozenset()
    assert len(remote.pulls) == window
    assert len(observations.older) == window - 1
    assert len(requests) <= 2 * (1 + window)
    assert peak < 5_000_000
    result = decide(observations)
    if revision > MAX_HISTORY + 1:
        assert result.state is State.UNKNOWN
        assert "HISTORY_TRUNCATED" in {f.code for f in result.findings}
    else:
        assert result.state is State.NO_RUN


def test_f5_superseded_detection_survives_inside_the_window(world: World) -> None:
    project = world.project()
    task = dataclasses.replace(project.tasks["APP-1"], revision=1000)
    older = f"work/APP-1-r{1000 - MAX_HISTORY}"
    responses = {f"{BASE}/git/matching-refs/heads/work/APP-1-r": [gh_ref(older)]}
    for r in range(1000 - MAX_HISTORY, 1001):
        responses[page(f"{BASE}/pulls", head=f"example-org:work/APP-1-r{r}", state="all")] = []
    responses[page(f"{BASE}/pulls", head=f"example-org:{older}", state="all")] = [gh_pull(5, older)]
    result = decide(observe(project, task, world.checkout, world.state, api(responses)[0]))
    assert "SUPERSEDED_PR_OPEN" in {f.code for f in result.findings}
    assert result.blocks_dispatch


# -- F6: CLI lock order -------------------------------------------------------------------------


def _fingerprint(*roots: Path) -> dict[str, Any]:
    found: dict[str, Any] = {}
    for root in roots:
        for path in sorted(root.rglob("*")):
            info = path.lstat()
            entry: Any = (info.st_mode, info.st_mtime_ns, info.st_size)
            if path.is_file() and not path.is_symlink():
                entry = (*entry, hashlib.sha256(path.read_bytes()).hexdigest())
            found[str(path)] = entry
    return found


@pytest.mark.parametrize("source", ["--live", "--evidence"])
def test_f6_cli_refuses_before_any_collection_when_the_lock_is_held(
    world: World, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    lock = world.state / PROJECT_ID / "dispatch.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("")
    snapshot = write_snapshot(ready_snapshot(world.project_dir), world.root / "snapshot.json")
    started: list[Any] = []

    def refuse(*args: Any, **_kwargs: Any) -> Any:
        started.append(args)
        raise AssertionError("process start attempted")

    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    transport = FakeTransport({})  # any request fails the test
    roots = (world.project_dir, world.checkout, world.state, world.archon)
    fd = os.open(lock, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        before = _fingerprint(*roots)
        args = ["preflight", world.project_dir, "--task", "APP-1", "--revision", "3", "--checkout", world.checkout]
        args += ["--live"] if source == "--live" else ["--evidence", snapshot]
        args += ["--state-dir", world.state, "--archon-home", world.archon, "--format", "json"]
        out, err = io.StringIO(), io.StringIO()
        code = run([str(a) for a in args], out=out, err=err, transport=transport)
        after = _fingerprint(*roots)
    finally:
        os.close(fd)
    assert code == 3, err.getvalue()
    assert transport.requests == []
    assert started == []
    assert after == before
    document = json.loads(out.getvalue())
    assert document["dryRun"]["reasonCodes"] == ["RECONCILE_COORDINATOR_ACTIVE"]
    assert document["command"] is None
    assert document["authorization"]["granted"] is False


def test_f6_reconcile_cli_refuses_without_requests(world: World) -> None:
    lock = world.state / PROJECT_ID / "dispatch.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("")
    transport = FakeTransport({})
    fd = os.open(lock, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        args = ["reconcile", world.project_dir, "--task", "APP-1", "--checkout", world.checkout, "--live"]
        args += ["--state-dir", world.state]
        code = run([str(a) for a in args], out=io.StringIO(), err=io.StringIO(), transport=transport)
    finally:
        os.close(fd)
    assert code == 3 and transport.requests == []


def test_f6_free_lock_keeps_the_normal_live_path(world: World) -> None:
    lock = world.state / PROJECT_ID / "dispatch.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("")
    transport = FakeTransport({})  # the live collection starts: its first request fails the fake
    args = ["preflight", world.project_dir, "--task", "APP-1", "--revision", "3", "--checkout", world.checkout]
    args += ["--live", "--state-dir", world.state, "--archon-home", world.archon]
    code = run([str(a) for a in args], out=io.StringIO(), err=io.StringIO(), transport=transport)
    assert transport.requests  # collection was attempted
    assert code != 0
