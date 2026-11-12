"""Observation integrity (S1-S5): invariant and metamorphic tests over the production chains.

Invariants under test (docs/architecture/archon-adapter.md, "Observation integrity"):

1. NO_RUN requires complete, coherent, affirmative evidence of absence.
2. Every relevant unknown or contradictory observation blocks reconciliation.
3. A selected filesystem object cannot silently be replaced between listing and reading.
4. Filesystem permissions are not taken as evidence of identity, and unsafe permissions never
   establish trust.
5. Stage 5 never authorizes or performs execution.

Everything runs on real files through ``read_task_records`` / ``read_checkout`` / ``factory
reconcile``; the GitHub transport is the only stand-in. Assertions are about outcomes (state,
blocking, exit code, which record was read), not about a reason-code string alone.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from github_support import FakeTransport
from stage5_support import DISPATCH, PROJECT_ID, intent, make_checkout, outcome, remote_responses, write_record
from support import copy_project, fake_sha

from factory import fsread, runs
from factory.cli import run
from factory.runs import read_task_records

R3 = "work/APP-1-r3"
GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def _reconcile(project: Path, checkout: Path, state: Path) -> tuple[int, dict[str, Any]]:
    args = ["reconcile", project, "--task", "APP-1", "--checkout", checkout, "--state-dir", state, "--live"]
    out = io.StringIO()
    code = run(
        [*map(str, args), "--format", "json"], out=out, err=io.StringIO(), transport=FakeTransport(remote_responses())
    )
    return code, json.loads(out.getvalue())


def _fingerprint(*roots: Path) -> dict[str, Any]:
    found: dict[str, Any] = {}
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            info = path.lstat()
            entry: Any = (info.st_mode, info.st_mtime_ns, info.st_size)
            if path.is_file() and not path.is_symlink():
                try:
                    entry = (*entry, hashlib.sha256(path.read_bytes()).hexdigest())
                except OSError:
                    pass
            found[str(path)] = entry
    return found


def _forbid_processes(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """From now on, any process start fails the test."""
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


# == S1: primary HEAD occupancy, real git and the real CLI ==========================================


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, env=GIT_ENV, capture_output=True, check=True)


def _real_checkout(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "--quiet", "--initial-branch=next")
    (path / ".archon").mkdir()
    (path / ".archon" / "config.yaml").write_text("worktree:\n  baseBranch: next\n")
    _git(path, "add", "-A")
    _git(path, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "--quiet", "-m", "c")
    _git(path, "remote", "add", "origin", "https://github.com/example-org/example-app.git")
    return path


@requires_git
@pytest.mark.parametrize(
    ("setup", "state", "code"),
    [
        (lambda c: (c / ".git" / "HEAD").write_text(f"ref: refs/heads/{R3}\n"), "UNCERTAIN", 3),
        (lambda c: _git(c, "checkout", "--quiet", "-b", R3), "UNTRACKED_LOCAL_BRANCH", 0),
        (lambda c: (c / ".git" / "HEAD").write_text("ref: refs/heads/bad..target\n"), "UNKNOWN", 3),
        (lambda c: (c / ".git" / "HEAD").unlink(), "UNKNOWN", 3),
        (lambda c: _git(c, "checkout", "--quiet", "--detach"), "NO_RUN", 0),
        (lambda c: _git(c, "worktree", "add", "--quiet", "-b", "other", str(c.parent / "wt-other")), "NO_RUN", 0),
        (
            lambda c: _git(c, "worktree", "add", "--quiet", "-b", R3, str(c.parent / "wt-feature")),
            "UNTRACKED_LOCAL_BRANCH",
            0,
        ),
    ],
    ids=[
        "dangling-head-to-feature",
        "feature-checked-out",
        "malformed-target",
        "missing-head",
        "detached",
        "unrelated-worktree",
        "feature-worktree",
    ],
)
def test_s1_head_occupancy_through_the_real_cli(tmp_path: Path, setup, state: str, code: int) -> None:  # type: ignore[no-untyped-def]
    project = copy_project(DISPATCH, tmp_path)
    checkout = _real_checkout(tmp_path / "checkout")
    setup(checkout)
    exit_code, document = _reconcile(project, checkout, tmp_path / "state")
    assert (document["state"], exit_code) == (state, code)
    assert document["blocksDispatch"] is (state != "NO_RUN")


@requires_git
def test_s1_head_changed_after_the_ref_listing_is_read_as_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HEAD rewritten before it is read: the read observes the new (dangling) HEAD, which blocks."""
    project = copy_project(DISPATCH, tmp_path)
    checkout = _real_checkout(tmp_path / "checkout")
    real_list = fsread.ConfinedTree.list
    done: list[bool] = []

    def listing(self, *parts):  # type: ignore[no-untyped-def]
        entries = real_list(self, *parts)
        if parts == (".git", "refs", "heads") and not done:
            done.append(True)
            (checkout / ".git" / "HEAD").write_text(f"ref: refs/heads/{R3}\n")
        return entries

    monkeypatch.setattr(fsread.ConfinedTree, "list", listing)
    exit_code, document = _reconcile(project, checkout, tmp_path / "state")
    assert done
    assert (document["state"], document["blocksDispatch"], exit_code) == ("UNCERTAIN", True, 3)


@requires_git
@pytest.mark.parametrize("change", ["in-place", "replaced"])
def test_s1_head_changed_after_it_was_read_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    """HEAD changed between being read and the coherence check: the checkout read is refused."""
    project = copy_project(DISPATCH, tmp_path)
    checkout = _real_checkout(tmp_path / "checkout")
    real_verify = fsread.ConfinedTree.verify
    done: list[bool] = []

    def verifying(self):  # type: ignore[no-untyped-def]
        if self.root == checkout.resolve() and not done:
            done.append(True)
            head = checkout / ".git" / "HEAD"
            if change == "in-place":
                head.write_text(f"ref: refs/heads/{R3}\n")
            else:
                (checkout / ".git" / "HEAD.new").write_text("ref: refs/heads/next\n")
                os.replace(checkout / ".git" / "HEAD.new", head)
        return real_verify(self)

    monkeypatch.setattr(fsread.ConfinedTree, "verify", verifying)
    exit_code, document = _reconcile(project, checkout, tmp_path / "state")
    assert done
    assert (document["state"], document["blocksDispatch"], exit_code) == ("UNKNOWN", True, 3)


# == S2: replacing observed objects at every step of reading run records ==============================


def _replacement(kind: str, outside: Path) -> Callable[[Path], None]:
    def create(target: Path) -> None:
        if kind == "empty-directory":
            target.mkdir()
        elif kind == "symlink":
            target.symlink_to(outside)
        elif kind == "regular-file":
            target.write_text("{}")
        elif kind == "fifo":
            os.mkfifo(target)
        elif kind == "other-valid-intent":
            shutil.copytree(outside, target)
        else:
            raise AssertionError(kind)

    return create


TARGETS = {
    # name in the record path, relative path of the object replaced
    "project": f"{PROJECT_ID}",
    "runs": f"{PROJECT_ID}/runs",
    "task": f"{PROJECT_ID}/runs/APP-1",
    "revision": f"{PROJECT_ID}/runs/APP-1/r3",
}
REPLACEMENTS = ["empty-directory", "symlink", "regular-file", "fifo", "other-valid-intent"]


def _substitute_tree(tmp_path: Path, relative: str) -> Path:
    """A tree shaped like ``relative`` holding a different valid intent (host 'substitute')."""
    other_state = tmp_path / "substitute-state"
    write_record(other_state, intent(host="substitute"))
    return other_state / relative


def _assert_original_or_refused(records: runs.TaskRecords) -> None:
    if not records.readable:
        assert records.problems  # an explicit refusal
        return
    last = records.revision(3).last
    assert last is not None and last.intent is not None
    assert last.intent["host"] == "coordinator"  # the original evidence, never the substitute


@pytest.mark.parametrize("target", sorted(TARGETS))
@pytest.mark.parametrize("replacement", REPLACEMENTS)
@pytest.mark.parametrize("moment", ["after-open", "before-open", "after-list"])
def test_s2_replaced_directory_never_yields_substituted_or_empty_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str, replacement: str, moment: str
) -> None:
    state = tmp_path / "state"
    write_record(state, intent())
    relative = TARGETS[target]
    victim = state / relative
    outside = _substitute_tree(tmp_path, relative)
    create = _replacement(replacement, outside)
    done: list[bool] = []

    def swap() -> None:
        if not done:
            done.append(True)
            os.rename(victim, tmp_path / f"moved-{target}")
            create(victim)

    name = victim.name
    real_open, real_list = os.open, fsread.ConfinedTree.list

    def opening(path, flags, *args, **kwargs):  # type: ignore[no-untyped-def]
        if moment == "before-open" and str(path) == name:
            swap()
        fd = real_open(path, flags, *args, **kwargs)
        if moment == "after-open" and str(path) == name:
            swap()
        return fd

    def listing(self, *parts):  # type: ignore[no-untyped-def]
        entries = real_list(self, *parts)
        if moment == "after-list" and parts == (PROJECT_ID, "runs", "APP-1"):
            swap()
        return entries

    monkeypatch.setattr(fsread.os, "open", opening)
    monkeypatch.setattr(fsread.ConfinedTree, "list", listing)
    records = read_task_records(state, PROJECT_ID, "APP-1")
    monkeypatch.undo()
    assert done, "the interleaving did not happen"
    _assert_original_or_refused(records)


@pytest.mark.parametrize("replacement", ["empty-directory-at-file", "symlink", "regular-file-other-intent", "fifo"])
def test_s2_attempt_file_replaced_between_listing_and_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replacement: str
) -> None:
    state = tmp_path / "state"
    path = write_record(state, intent())
    other = tmp_path / "other.json"
    other.write_text(json.dumps(intent(host="substitute")))
    real_list = fsread.ConfinedTree.list
    done: list[bool] = []

    def listing(self, *parts):  # type: ignore[no-untyped-def]
        entries = real_list(self, *parts)
        if parts and parts[-1] == "r3" and not done:
            done.append(True)
            os.rename(path, tmp_path / "moved.json")
            if replacement == "empty-directory-at-file":
                path.mkdir()
            elif replacement == "symlink":
                path.symlink_to(other)
            elif replacement == "regular-file-other-intent":
                shutil.copy(other, path)
            else:
                os.mkfifo(path)
        return entries

    monkeypatch.setattr(fsread.ConfinedTree, "list", listing)
    records = read_task_records(state, PROJECT_ID, "APP-1")
    monkeypatch.undo()
    assert done
    _assert_original_or_refused(records)
    assert not records.readable  # the listed file is gone: the observation is refused


def test_s2_record_changed_after_it_was_read_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = tmp_path / "state"
    path = write_record(state, intent())
    real_parse = runs.parse_json
    done: list[bool] = []

    def parsing(text: str):  # type: ignore[no-untyped-def]
        if not done:
            done.append(True)
            path.write_text(json.dumps(intent(host="substitute")))  # in place, during parsing
        return real_parse(text)

    monkeypatch.setattr(runs, "parse_json", parsing)
    records = read_task_records(state, PROJECT_ID, "APP-1")
    assert done and not records.readable


def test_s2_entry_added_during_enumeration_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = tmp_path / "state"
    write_record(state, intent())
    revision = state / PROJECT_ID / "runs" / "APP-1" / "r3"
    real_scandir = os.scandir
    done: list[bool] = []

    def scanning(fd):  # type: ignore[no-untyped-def]
        iterator = real_scandir(fd)
        if not done and os.fstat(fd).st_ino == revision.stat().st_ino:
            done.append(True)
            (revision / "attempt-2.intent.json").write_text(json.dumps(intent(attempt=2)))
        return iterator

    monkeypatch.setattr(fsread.os, "scandir", scanning)
    records = read_task_records(state, PROJECT_ID, "APP-1")
    assert done and not records.readable
    assert {p.code for p in records.problems} <= set(fsread.INTEGRITY_CODES)


def test_s2_replaced_records_through_the_cli_never_reconcile_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = copy_project(DISPATCH, tmp_path)
    checkout = make_checkout(tmp_path / "checkout")
    state = tmp_path / "state"
    write_record(state, intent())
    real_list = fsread.ConfinedTree.list
    done: list[bool] = []

    def listing(self, *parts):  # type: ignore[no-untyped-def]
        entries = real_list(self, *parts)
        if parts == (PROJECT_ID, "runs", "APP-1") and not done:
            done.append(True)
            task = state / PROJECT_ID / "runs" / "APP-1"
            os.rename(task / "r3", tmp_path / "r3-moved")
            (task / "r3").mkdir()
        return entries

    monkeypatch.setattr(fsread.ConfinedTree, "list", listing)
    exit_code, document = _reconcile(project, checkout, state)
    assert done
    assert document["state"] != "NO_RUN" and document["blocksDispatch"] and exit_code == 3


# == Properties over many scenarios ==========================================================


def _world(tmp_path: Path, scenario: str) -> dict[str, Path]:
    project = copy_project(DISPATCH, tmp_path)
    branches = {R3: fake_sha("local")} if scenario == "local-branch" else {}
    checkout = make_checkout(tmp_path / "checkout", branches=branches)
    state = tmp_path / "state"
    if scenario == "interrupted":
        write_record(state, intent())
    elif scenario == "no-pr":
        write_record(state, intent())
        write_record(state, outcome())
    elif scenario == "clean":
        (state / PROJECT_ID / "runs" / "APP-1").mkdir(parents=True)  # an existing, empty history
    return {"project": project, "checkout": checkout, "state": state}


BASELINES = {"clean": "NO_RUN", "interrupted": "UNCERTAIN", "local-branch": "UNTRACKED_LOCAL_BRANCH", "no-pr": "NO_PR"}

# Objects whose observation the decision depends on, per scenario (relative to the world roots).
OBSERVED = {
    "clean": [
        "checkout:.git/HEAD",
        "checkout:.git/refs/heads",
        "checkout:.git/config",
        "state:example-dispatch/runs/APP-1",
    ],
    "interrupted": [
        "checkout:.git/HEAD",
        "state:example-dispatch/runs/APP-1/r3",
        "state:example-dispatch/runs/APP-1/r3/attempt-1.intent.json",
    ],
    "local-branch": ["checkout:.git/HEAD", f"checkout:.git/refs/heads/{R3}", "checkout:.git/refs/heads"],
    "no-pr": ["state:example-dispatch/runs/APP-1/r3/attempt-1.outcome.json", "state:example-dispatch/runs/APP-1/r3"],
}


def _mutate(path: Path, how: str) -> None:
    if how == "unreadable":
        os.chmod(path, 0)
    elif how == "garbage":
        if path.is_dir():
            shutil.rmtree(path)
        path.write_bytes(b"\x00garbage\xff")
    elif how == "fifo":
        shutil.rmtree(path) if path.is_dir() else path.unlink()
        os.mkfifo(path)
    elif how == "symlink":
        shutil.rmtree(path) if path.is_dir() else path.unlink()
        path.symlink_to("/")
    elif how == "group-writable":
        os.chmod(path, (path.stat().st_mode & 0o777) | 0o020)
    elif how == "acl" and sys.platform == "darwin":
        subprocess.run(["chmod", "+a", "everyone allow write,add_file,delete_child", str(path)], check=True)
    elif how == "acl":
        pytest.skip("ACL mutation is exercised on macOS")
    else:
        raise AssertionError(how)


@pytest.mark.parametrize("scenario", sorted(BASELINES))
def test_property_baselines(tmp_path: Path, scenario: str) -> None:
    world = _world(tmp_path, scenario)
    _, document = _reconcile(world["project"], world["checkout"], world["state"])
    assert document["state"] == BASELINES[scenario]


@pytest.mark.parametrize("how", ["unreadable", "garbage", "fifo", "symlink", "group-writable", "acl"])
@pytest.mark.parametrize(("scenario", "target"), [(s, t) for s, ts in sorted(OBSERVED.items()) for t in ts])
def test_property_degrading_evidence_never_increases_confidence(
    tmp_path: Path, scenario: str, target: str, how: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Properties 1, 2, 4, 6: making an observed object unreadable, corrupt, a special file, a link or
    unsafely writable never produces NO_RUN, and never unblocks a blocking baseline."""
    world = _world(tmp_path, scenario)
    root, relative = target.split(":", 1)
    path = world[root] / relative
    if how == "unreadable" and os.geteuid() == 0:
        pytest.skip("root ignores permissions")
    _mutate(path, how)
    no_processes = _forbid_processes(monkeypatch)
    try:
        before = _fingerprint(world["project"], world["checkout"], world["state"])
        exit_code, document = _reconcile(world["project"], world["checkout"], world["state"])
        after = _fingerprint(world["project"], world["checkout"], world["state"])
    finally:
        if how == "unreadable":
            os.chmod(path, 0o755)
    assert document["state"] != "NO_RUN", (scenario, target, how, document["findings"])
    assert document["blocksDispatch"] is True
    if document["state"] in ("UNKNOWN", "UNCERTAIN"):
        assert exit_code == 3
    assert after == before  # property 10: nothing was written
    assert no_processes == []  # property 9: nothing was started


@pytest.mark.parametrize("scenario", sorted(BASELINES))
def test_property_clean_result_requires_affirmative_absence(tmp_path: Path, scenario: str) -> None:
    """Property 4: offline, nothing remote is known, so no scenario can reconcile clean."""
    world = _world(tmp_path, scenario)
    args = ["reconcile", world["project"], "--task", "APP-1", "--checkout", world["checkout"]]
    out = io.StringIO()
    code = run(
        [*map(str, args), "--state-dir", str(world["state"]), "--offline", "--format", "json"],
        out=out,
        err=io.StringIO(),
    )
    document = json.loads(out.getvalue())
    assert document["state"] != "NO_RUN" and document["blocksDispatch"] and code == 3


def test_property_contradictions_never_reconcile_clean(tmp_path: Path) -> None:
    """Property 5: an outcome whose intent is missing, or records for a newer revision, are contradictions."""
    for name, records in [
        ("outcome-without-intent", [outcome()]),
        ("newer-revision", [intent(revision=4)]),
        ("gap", [intent(attempt=1), outcome(attempt=1), intent(attempt=3), outcome(attempt=3)]),
    ]:
        world = _world(tmp_path / name, "clean")
        for record in records:
            write_record(world["state"], record)
        code, document = _reconcile(world["project"], world["checkout"], world["state"])
        assert (document["state"], code) == ("UNCERTAIN", 3), name


@pytest.mark.parametrize(
    "exhaust",
    ["entries", "yaml-depth", "yaml-size"],
)
def test_property_resource_exhaustion_is_a_controlled_refusal(tmp_path: Path, exhaust: str) -> None:
    """Property 7: budgets and parser bounds refuse; nothing crashes (no exit 70) or reads as empty."""
    world = _world(tmp_path, "interrupted")
    if exhaust == "entries":
        revision = world["state"] / PROJECT_ID / "runs" / "APP-1" / "r3"
        for index in range(runs.MAX_RECORD_OBJECTS + 1):
            (revision / f"stray-{index}").write_bytes(b"")
        code, document = _reconcile(world["project"], world["checkout"], world["state"])
        assert document["state"] != "NO_RUN" and code == 3
        records = read_task_records(world["state"], PROJECT_ID, "APP-1")
        assert not records.readable and {p.code for p in records.problems} == {"BUDGET_EXCEEDED"}
        return
    if exhaust == "yaml-size":
        task = world["project"] / "factory" / "tasks" / "APP-2.yaml"
        task.write_text("big: [" + "1," * 250_000 + "1]\n" + task.read_text())
        out, err = io.StringIO(), io.StringIO()
        code = run(["validate", str(world["project"]), "--format", "json"], out=out, err=err)
        assert code == 2, err.getvalue()
        assert "YAML_TOO_COMPLEX" in {e["code"] for e in json.loads(out.getvalue())["errors"]}
        return
    config = world["checkout"] / ".archon" / "config.yaml"
    config.write_text("a: " + "[" * 5000 + "]" * 5000 + "\n")
    snapshot = tmp_path / "snapshot.json"
    from stage5_support import make_archon_home, ready_snapshot

    snapshot.write_text(json.dumps(ready_snapshot(world["project"])))
    out, err = io.StringIO(), io.StringIO()
    args = ["preflight", world["project"], "--task", "APP-1", "--revision", "3", "--checkout", world["checkout"]]
    args += [
        "--evidence",
        snapshot,
        "--state-dir",
        world["state"],
        "--archon-home",
        make_archon_home(tmp_path / "archon"),
    ]
    code = run([*map(str, args), "--format", "json"], out=out, err=err)
    assert code == 1, err.getvalue()
    document = json.loads(out.getvalue())
    assert "ARCHON_REPO_CONFIG_INVALID" in document["dryRun"]["reasonCodes"]
    assert document["authorization"]["granted"] is False
    # The same document as a project file: validation refuses it (exit 2), it does not crash (70).
    task = world["project"] / "factory" / "tasks" / "APP-2.yaml"
    task.write_text("deep: " + "[" * 5000 + "]" * 5000 + "\n")
    assert run(["validate", str(world["project"])], out=io.StringIO(), err=io.StringIO()) == 2


def test_property_authorization_is_false_in_every_report(tmp_path: Path) -> None:
    """Property 8: every preflight produced across the scenarios has authorization.granted false."""
    from stage5_support import make_archon_home, ready_snapshot

    for scenario in sorted(BASELINES):
        world = _world(tmp_path / scenario, scenario)
        snapshot = tmp_path / f"{scenario}.json"
        snapshot.write_text(json.dumps(ready_snapshot(world["project"])))
        out = io.StringIO()
        args = ["preflight", world["project"], "--task", "APP-1", "--revision", "3", "--checkout", world["checkout"]]
        args += [
            "--evidence",
            snapshot,
            "--state-dir",
            world["state"],
            "--archon-home",
            make_archon_home(tmp_path / f"a-{scenario}"),
        ]
        code = run([*map(str, args), "--format", "json"], out=out, err=io.StringIO())
        document = json.loads(out.getvalue())
        assert document["authorization"]["granted"] is False
        assert document["dryRun"]["result"] != "PASS"
        assert code != 0
