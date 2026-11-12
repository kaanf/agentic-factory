"""Regression tests for the second Stage 5 security review: R1, R2, R3.

R1 and R2 run through the real production chains (real git checkouts made by git, real run-record
files, the real ``factory reconcile`` / ``factory preflight`` entry points); the GitHub transport is
the only stand-in. R3 tests the structured repository-configuration guard on its own and through
preflight, without executing Archon.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from github_support import FakeTransport
from stage5_support import (
    DISPATCH,
    ORIGIN,
    PROJECT_ID,
    api,
    intent,
    make_archon_home,
    make_checkout,
    preflight_responses,
    ready_snapshot,
    remote_responses,
    write_record,
)
from support import copy_project

from factory import archon, fsread
from factory import preflight as preflight_module
from factory.cli import run
from factory.evidence import parse_snapshot
from factory.planning import evaluate
from factory.preflight import preflight
from factory.project import load_project
from factory.reconcile import State, decide, observe
from factory.runs import read_task_records

R3 = "work/APP-1-r3"
GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, env=GIT_ENV, capture_output=True, check=True)


def _real_checkout(path: Path) -> Path:
    """A real git repository made by git, with the fixture's origin and Archon configuration."""
    path.mkdir(parents=True)
    _git(path, "init", "--quiet", "--initial-branch=next")
    (path / ".archon").mkdir()
    (path / ".archon" / "config.yaml").write_text("worktree:\n  baseBranch: next\n")
    _git(path, "add", "-A")
    _git(path, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "--quiet", "-m", "c")
    _git(path, "remote", "add", "origin", ORIGIN)
    return path


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


def _no_processes(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    started: list[Any] = []

    def refuse(*args: Any, **_kwargs: Any) -> Any:
        started.append(args)
        raise AssertionError("process start attempted")

    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    for name in ("system", "posix_spawn", "posix_spawnp", "execv", "execve", "fork"):
        if hasattr(os, name):
            monkeypatch.setattr(os, name, refuse)
    return started


def _reconcile_cli(project: Path, checkout: Path, state: Path, *, live: bool = True) -> tuple[int, dict[str, Any]]:
    args = ["reconcile", project, "--task", "APP-1", "--checkout", checkout, "--state-dir", state, "--format", "json"]
    args.append("--live" if live else "--offline")
    out = io.StringIO()
    code = run([str(a) for a in args], out=out, err=io.StringIO(), transport=FakeTransport(remote_responses()))
    return code, json.loads(out.getvalue())


@pytest.fixture
def project(tmp_path: Path) -> Path:
    return copy_project(DISPATCH, tmp_path)


# -- R1: corrupted primary HEAD ------------------------------------------------------------------


@requires_git
@pytest.mark.parametrize(
    "head",
    ["garbage-not-a-ref\n", "", "ref: refs/heads/bad..name\n", "ref: refs/tags/v1\n", "ref: \n", "abc123\n"],
    ids=["garbage", "empty", "bad-branch-name", "non-branch-symref", "empty-symref", "short-sha"],
)
def test_r1_malformed_head_blocks_through_the_real_cli(
    tmp_path: Path, project: Path, monkeypatch: pytest.MonkeyPatch, head: str
) -> None:
    checkout = _real_checkout(tmp_path / "checkout")
    (checkout / ".git" / "HEAD").write_text(head)
    state = tmp_path / "state"
    before = _fingerprint(project, checkout)
    started = _no_processes(monkeypatch)
    code, document = _reconcile_cli(project, checkout, state)
    assert (code, document["state"], document["blocksDispatch"]) == (3, "UNKNOWN", True)
    assert "CHECKOUT_HEAD_UNKNOWN" in {f["code"] for f in document["findings"]}
    assert _fingerprint(project, checkout) == before
    assert started == [] and not state.exists()


@requires_git
@pytest.mark.parametrize("how", ["missing", "directory", "unreadable", "symlink"])
def test_r1_missing_or_unreadable_head_blocks(tmp_path: Path, project: Path, how: str) -> None:
    checkout = _real_checkout(tmp_path / "checkout")
    head = checkout / ".git" / "HEAD"
    head.unlink()
    if how == "directory":
        head.mkdir()
    elif how == "unreadable":
        head.write_text("ref: refs/heads/next\n")
        os.chmod(head, 0)
    elif how == "symlink":
        (tmp_path / "elsewhere").write_text("ref: refs/heads/next\n")
        head.symlink_to(tmp_path / "elsewhere")
    try:
        if how == "unreadable" and os.access(head, os.R_OK):
            pytest.skip("running with privileges that ignore file permissions")
        code, document = _reconcile_cli(project, checkout, tmp_path / "state")
    finally:
        if how == "unreadable":
            os.chmod(head, 0o644)
    assert (code, document["state"], document["blocksDispatch"]) == (3, "UNKNOWN", True)


@requires_git
def test_r1_valid_detached_head_still_reconciles_clean(tmp_path: Path, project: Path) -> None:
    checkout = _real_checkout(tmp_path / "checkout")
    _git(checkout, "checkout", "--quiet", "--detach")
    code, document = _reconcile_cli(project, checkout, tmp_path / "state")
    assert (code, document["state"], document["blocksDispatch"]) == (0, "NO_RUN", False)


@requires_git
def test_r1_valid_linked_worktrees(tmp_path: Path, project: Path) -> None:
    checkout = _real_checkout(tmp_path / "checkout")
    _git(checkout, "worktree", "add", "--quiet", "-b", "unrelated", str(tmp_path / "wt-unrelated"))
    code, document = _reconcile_cli(project, checkout, tmp_path / "state")
    assert (code, document["state"]) == (0, "NO_RUN")
    _git(checkout, "worktree", "add", "--quiet", "-b", R3, str(tmp_path / "wt-feature"))
    code, document = _reconcile_cli(project, checkout, tmp_path / "state")
    assert (code, document["state"], document["blocksDispatch"]) == (0, "UNTRACKED_LOCAL_BRANCH", True)


@requires_git
def test_r1_preflight_and_reconcile_agree(tmp_path: Path, project: Path) -> None:
    checkout = _real_checkout(tmp_path / "checkout")
    (checkout / ".git" / "HEAD").write_text("garbage\n")
    loaded = load_project(project).project
    assert loaded is not None
    snapshot = parse_snapshot(ready_snapshot(project), loaded, "s").snapshot
    github, _ = api(preflight_responses())
    report = preflight(
        loaded, "APP-1", 3, checkout, tmp_path / "state", make_archon_home(tmp_path / "archon"),
        evaluate(loaded, snapshot), github,
    )  # fmt: skip
    codes = set(report.to_dict()["dryRun"]["reasonCodes"])
    assert {"CHECKOUT_HEAD_UNKNOWN", "RECONCILE_UNKNOWN"} <= codes
    assert report.to_dict()["reconciliation"]["state"] == "UNKNOWN"
    assert report.authorization.granted is False


# -- R2: root confinement through run records and reconciliation -----------------------------------


def _decide(project: Path, checkout: Path, state: Path):  # type: ignore[no-untyped-def]
    loaded = load_project(project).project
    assert loaded is not None
    return decide(observe(loaded, loaded.tasks["APP-1"], checkout, state, api(remote_responses())[0]))


def test_r2_symlinked_state_root_never_becomes_an_empty_history(tmp_path: Path, project: Path) -> None:
    real = tmp_path / "real-state"
    write_record(real, intent())
    link = tmp_path / "configured-state"
    link.symlink_to(real)
    records = read_task_records(link, PROJECT_ID, "APP-1")
    assert records.readable is False
    assert {p.code for p in records.problems} == {"STATE_UNREADABLE"}
    result = _decide(project, make_checkout(tmp_path / "checkout"), link)
    assert result.state is not State.NO_RUN and result.blocks_dispatch
    code, document = _reconcile_cli(project, tmp_path / "checkout", link)
    assert code == 3 and document["state"] in ("UNKNOWN", "UNCERTAIN")


def test_r2_ancestor_swap_during_record_reading_is_refused(
    tmp_path: Path, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = tmp_path / "a" / "state"
    write_record(real, intent())
    evil = tmp_path / "evil" / "state"
    evil.mkdir(parents=True)
    real_open = os.open
    done: list[bool] = []

    def opening(path, flags, *args, **kwargs):  # type: ignore[no-untyped-def]
        fd = real_open(path, flags, *args, **kwargs)
        if str(path) == "a" and not done:
            done.append(True)
            os.rename(tmp_path / "a", tmp_path / "a-moved")
            os.rename(tmp_path / "evil", tmp_path / "a")
        return fd

    monkeypatch.setattr(fsread.os, "open", opening)
    records = read_task_records(real, PROJECT_ID, "APP-1")
    monkeypatch.undo()
    assert done and records.readable is False  # never the attacker's empty tree


def test_r2_shared_writable_ancestor_of_state_is_refused(tmp_path: Path, project: Path) -> None:
    shared = tmp_path / "shared"
    state = shared / "state"
    write_record(state, intent())
    os.chmod(shared, 0o777)
    try:
        assert read_task_records(state, PROJECT_ID, "APP-1").readable is False
        assert _decide(project, make_checkout(tmp_path / "checkout"), state).state is not State.NO_RUN
    finally:
        os.chmod(shared, 0o755)


def test_r2_symlinked_checkout_and_archon_home_are_unknown(tmp_path: Path, project: Path) -> None:
    real = make_checkout(tmp_path / "real-checkout")
    (tmp_path / "checkout-link").symlink_to(real)
    result = _decide(project, tmp_path / "checkout-link", tmp_path / "state")
    assert result.state is State.UNKNOWN
    assert "CHECKOUT_UNREADABLE" in {f.code for f in result.findings}
    home = make_archon_home(tmp_path / "real-archon")
    (tmp_path / "archon-link").symlink_to(home)
    install = archon.read_archon_home(tmp_path / "archon-link")
    assert install.version is None and install.version_problem is not None
    assert install.version_problem.code == "ARCHON_HOME_UNREADABLE"


def test_r2_valid_state_still_reads(tmp_path: Path, project: Path) -> None:
    state = tmp_path / "state"
    write_record(state, intent())
    records = read_task_records(state, PROJECT_ID, "APP-1")
    assert records.readable and records.revision(3).last is not None
    assert _decide(project, make_checkout(tmp_path / "checkout"), state).state is State.UNCERTAIN


# -- R3: structured repository configuration guard ------------------------------------------------


def _codes(text: str) -> set[str]:
    return {f.code for f in archon.check_repo_config_keys(text, "local")}


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("worktree:\n  baseBranch: next\n  postCreate: ./x.sh\n", "ARCHON_REPO_HOOK_PRESENT"),
        ("worktree: {baseBranch: next, postCreate: ./x.sh}\n", "ARCHON_REPO_HOOK_PRESENT"),
        ('worktree:\n  "postCreate": ./x.sh\n', "ARCHON_REPO_HOOK_PRESENT"),
        ("worktree:\n  'copyFiles': [.env]\n", "ARCHON_REPO_HOOK_PRESENT"),
        ('{"worktree": {"baseBranch": "next", "postCreate": "./x.sh"}}\n', "ARCHON_REPO_HOOK_PRESENT"),
        ("worktree:\n  ? postCreate\n  : ./x.sh\n", "ARCHON_REPO_HOOK_PRESENT"),
        ("worktree:\n  initSubmodules: true\n", "ARCHON_REPO_HOOK_PRESENT"),
        ("worktree:\n  postCreateTimeout: 10\n", "ARCHON_REPO_HOOK_PRESENT"),
        ("hooks: &h {postCreate: ./x.sh}\nworktree:\n  <<: *h\n", "ARCHON_REPO_CONFIG_AMBIGUOUS"),
        ("worktree:\n  <<: {postCreate: ./x.sh}\n", "ARCHON_REPO_CONFIG_AMBIGUOUS"),
        ("base: &b next\nworktree:\n  baseBranch: *b\n", "ARCHON_REPO_CONFIG_AMBIGUOUS"),
        ("worktree:\n  !!str postCreate : ./x.sh\n", "ARCHON_REPO_CONFIG_AMBIGUOUS"),
        ("worktree: !!map {baseBranch: next}\n", "ARCHON_REPO_CONFIG_AMBIGUOUS"),
        ("worktree:\n  baseBranch: next\n  baseBranch: stable\n", "ARCHON_REPO_CONFIG_INVALID"),
        ("worktree:\n  baseBranch: next\nworktree:\n  postCreate: x\n", "ARCHON_REPO_CONFIG_INVALID"),
        ("worktree: [\n", "ARCHON_REPO_CONFIG_INVALID"),
        ("--- \nworktree: {}\n--- \nworktree: {postCreate: x}\n", "ARCHON_REPO_CONFIG_INVALID"),
        ("- worktree\n", "ARCHON_REPO_CONFIG_INVALID"),
        ("worktree: next\n", "ARCHON_REPO_CONFIG_INVALID"),
        ("worktree:\n  baseBranch: next\nenv:\n  DEFAULT_AI_ASSISTANT: codex\n", "ARCHON_REPO_CONFIG_KEY_NOT_ALLOWED"),
        ("worktree:\n  baseBranch: next\n  remote: upstream\n", "ARCHON_REPO_CONFIG_KEY_NOT_ALLOWED"),
        ("worktree:\n  baseBranch: next\n  path: /tmp/x\n", "ARCHON_REPO_CONFIG_KEY_NOT_ALLOWED"),
        ("worktree:\n  baseBranch: next\nworkflows: {x: y}\n", "ARCHON_REPO_CONFIG_KEY_NOT_ALLOWED"),
        (
            "worktree:\n  baseBranch: next\ndefaults: {loadDefaultWorkflows: false}\n",
            "ARCHON_REPO_CONFIG_KEY_NOT_ALLOWED",
        ),
        ("worktree:\n  baseBranch: next\ncommands: {folder: x}\n", "ARCHON_REPO_CONFIG_KEY_NOT_ALLOWED"),
        ("worktree:\n  baseBranch: next\ncontainer: {image: x}\n", "ARCHON_REPO_CONFIG_KEY_NOT_ALLOWED"),
        ("worktree:\n  baseBranch: next\nassistants: {claude: {x: y}}\n", "ARCHON_REPO_CONFIG_KEY_NOT_ALLOWED"),
        ("worktree:\n  baseBranch: next\n1: x\n", "ARCHON_REPO_CONFIG_KEY_NOT_ALLOWED"),
    ],
)
def test_r3_guard_refuses_every_spelling(text: str, code: str) -> None:
    assert code in _codes(text)


@pytest.mark.parametrize(
    "text",
    [
        "worktree:\n  baseBranch: next\n",
        "worktree: {baseBranch: next}\n",
        '{"worktree": {"baseBranch": "next"}}\n',
        "# comment\nworktree:\n  baseBranch: 'next'\nassistant: claude\n",
        "worktree:\n  baseBranch: next\ntiers:\n  large: {provider: claude, model: opus}\n",
    ],
)
def test_r3_valid_minimal_configurations_pass_the_guard(text: str) -> None:
    assert _codes(text) == set()


@pytest.fixture
def world(tmp_path: Path, project: Path) -> dict[str, Any]:
    loaded = load_project(project).project
    assert loaded is not None
    snapshot = parse_snapshot(ready_snapshot(project), loaded, "s").snapshot
    return {
        "project": loaded,
        "plan": evaluate(loaded, snapshot),
        "checkout": make_checkout(tmp_path / "checkout"),
        "archon": make_archon_home(tmp_path / "archon"),
        "state": tmp_path / "state",
    }


def _preflight(world: dict[str, Any], remote: str = "worktree:\n  baseBranch: next\n"):  # type: ignore[no-untyped-def]
    github, _ = api(preflight_responses(remote_config=remote))
    return preflight(
        world["project"], "APP-1", 3, world["checkout"], world["state"], world["archon"], world["plan"], github
    )


@pytest.mark.parametrize(
    ("local", "remote", "code"),
    [
        ("worktree: {baseBranch: next, postCreate: ./x.sh}\n", None, "ARCHON_REPO_HOOK_PRESENT"),
        ("hooks: &h {postCreate: x}\nworktree:\n  baseBranch: next\n  <<: *h\n", None, "ARCHON_REPO_CONFIG_AMBIGUOUS"),
        (None, '{"worktree": {"baseBranch": "next", "postCreate": "x"}}\n', "ARCHON_REPO_HOOK_PRESENT"),
        ("worktree:\n  baseBranch: next\nenv: {A: b}\n", None, "ARCHON_REPO_CONFIG_KEY_NOT_ALLOWED"),
    ],
)
def test_r3_guard_through_preflight(world: dict[str, Any], local: str | None, remote: str | None, code: str) -> None:
    if local is not None:
        (world["checkout"] / ".archon" / "config.yaml").write_text(local)
    report = _preflight(world, remote or "worktree:\n  baseBranch: next\n")
    document = report.to_dict()
    assert code in document["dryRun"]["reasonCodes"]
    assert report.result.value == "FAIL"
    assert report.authorization.granted is False


def test_r3_configuration_swap_during_preflight_is_detected(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    config = world["checkout"] / ".archon" / "config.yaml"
    original = preflight_module.decide

    def swap(observations):  # type: ignore[no-untyped-def]
        config.write_text("worktree:\n  baseBranch: next\n  postCreate: ./x.sh\n")
        return original(observations)

    monkeypatch.setattr(preflight_module, "decide", swap)
    report = _preflight(world)
    document = report.to_dict()
    assert "INPUT_CHANGED_DURING_PREFLIGHT" in document["dryRun"]["reasonCodes"]
    assert (
        document["inputs"]["archonRepoConfigSha256"] == hashlib.sha256(b"worktree:\n  baseBranch: next\n").hexdigest()
    )


def test_r3_valid_configuration_has_no_guard_findings(world: dict[str, Any]) -> None:
    codes = set(_preflight(world).to_dict()["dryRun"]["reasonCodes"])
    assert not {c for c in codes if c.startswith(("ARCHON_REPO_", "INPUT_CHANGED"))}
    assert codes == {"ARCHON_DATABASE_AI_CONFIGURATION_UNOBSERVED"}  # still never PASS (provider)
