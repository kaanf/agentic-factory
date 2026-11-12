"""Fourth review (Q1-Q5): what is detected, what is refused, and what is explicitly not guaranteed.

Kinds of claims kept apart:

- **Best-effort detections** (Q2, and Q1 changes before the re-check reaches an object): a change
  between observation and ``verify()`` is refused. Tested at specific points; not a proof of
  atomicity.
- **Deliberate fail-closed behavior** (Q3, Q4, Q5): conditions that are always refused.
- **Unsupported concurrency conditions** (Q1, cross-observer changes): changes made after an
  observation completed are not seen; a read-only observer cannot see them. Encoded as strict xfail
  tests of the property one might want, pending decision 0009 (proposed); they fail loudly if the
  behavior ever changes, and claim nothing.

Real files and the real CLI throughout; GitHub is the only stand-in.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
import yaml
from github_support import FakeTransport
from stage5_support import (
    DISPATCH,
    PROJECT_ID,
    api,
    intent,
    make_archon_home,
    make_checkout,
    preflight_responses,
    ready_snapshot,
    record_dir,
    remote_responses,
    write_record,
)
from support import copy_project, fake_sha

from factory import documents, fsread
from factory.cli import run
from factory.documents import DocumentError, parse_json, parse_yaml
from factory.evidence import parse_snapshot
from factory.planning import evaluate
from factory.preflight import preflight
from factory.project import load_project
from factory.runs import read_task_records

R3 = "work/APP-1-r3"
PENDING_0009 = "decision 0009 (proposed): a read-only observer cannot see changes made after it observed"


def _reconcile(project: Path, checkout: Path, state: Path, *, live: bool = True) -> tuple[int, dict[str, Any]]:
    args = ["reconcile", project, "--task", "APP-1", "--checkout", checkout, "--state-dir", state]
    args.append("--live" if live else "--offline")
    out = io.StringIO()
    code = run(
        [*map(str, args), "--format", "json"], out=out, err=io.StringIO(), transport=FakeTransport(remote_responses())
    )
    return code, json.loads(out.getvalue())


@pytest.fixture
def world(tmp_path: Path) -> dict[str, Path]:
    state = tmp_path / "state"
    (state / PROJECT_ID / "runs" / "APP-1" / "r2").mkdir(parents=True)
    (state / PROJECT_ID / "runs" / "APP-1" / "r3").mkdir()
    return {
        "tmp": tmp_path,
        "project": copy_project(DISPATCH, tmp_path),
        "checkout": make_checkout(tmp_path / "checkout"),
        "state": state,
    }


class _DuringVerify:
    """Run ``action`` once when ``verify()`` walks to ``target`` (sorted order: r2 before r3)."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, target: str, action) -> None:  # type: ignore[no-untyped-def]
        self.done = False
        real_walk, real_verify = fsread.ConfinedTree._walk, fsread.ConfinedTree.verify
        state = {"inside": False}

        def walk(tree, parts):  # type: ignore[no-untyped-def]
            if state["inside"] and not self.done and parts and parts[-1] == target:
                self.done = True
                action()
            return real_walk(tree, parts)

        def verify(tree):  # type: ignore[no-untyped-def]
            state["inside"] = True
            try:
                return real_verify(tree)
            finally:
                state["inside"] = False

        monkeypatch.setattr(fsread.ConfinedTree, "_walk", walk)
        monkeypatch.setattr(fsread.ConfinedTree, "verify", verify)


# == Q1: what verify() detects, and what it cannot =============================================


def test_q1_change_before_the_recheck_reaches_the_object_is_refused(world, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Best-effort detection: an intent inserted into r3 before verify() re-checks r3."""
    hook = _DuringVerify(monkeypatch, "r2", lambda: write_record(world["state"], intent(revision=3)))
    code, document = _reconcile(world["project"], world["checkout"], world["state"])
    assert hook.done
    assert (document["state"], document["blocksDispatch"], code) == ("UNKNOWN", True, 3)


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=PENDING_0009)
def test_q1_change_after_the_recheck_passed_the_object_is_not_seen(world, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The reproduced schedule: r2 re-checked, then an intent appears in r2, then r3 re-checked."""
    hook = _DuringVerify(monkeypatch, "r3", lambda: write_record(world["state"], intent(revision=2)))
    code, document = _reconcile(world["project"], world["checkout"], world["state"])
    assert hook.done and (record_dir(world["state"], revision=2) / "attempt-1.intent.json").exists()
    assert document["state"] != "NO_RUN"  # the property one might want; not provided


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=PENDING_0009)
@pytest.mark.parametrize("change", ["records-after-read", "branch-after-checkout-read", "root-appears-after-read"])
def test_q1_changes_between_independent_observers_are_not_seen(world, monkeypatch, change: str) -> None:  # type: ignore[no-untyped-def]
    """Lock, records, checkout and GitHub are observed at different moments; no common snapshot."""
    import factory.reconcile as reconcile_module

    if change == "root-appears-after-read":
        os.rename(world["state"], world["tmp"] / "state-later")
    real_read_remote = reconcile_module.read_remote

    def later(api_, repo, task):  # type: ignore[no-untyped-def]
        if change == "records-after-read":
            write_record(world["state"], intent())
        elif change == "branch-after-checkout-read":
            path = world["checkout"] / ".git" / "refs" / "heads" / "work" / "APP-1-r3"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(fake_sha("late") + "\n")
        else:
            os.rename(world["tmp"] / "state-later", world["state"])
            write_record(world["state"], intent())
        return real_read_remote(api_, repo, task)

    monkeypatch.setattr(reconcile_module, "read_remote", later)
    _, document = _reconcile(world["project"], world["checkout"], world["state"])
    assert document["state"] != "NO_RUN"  # the property one might want; not provided


# == Q2: root namespace and continuing trust (best-effort detections) ===================================


def _read_with_change_before_verify(state: Path, monkeypatch: pytest.MonkeyPatch, action) -> Any:  # type: ignore[no-untyped-def]
    real_verify = fsread.ConfinedTree.verify

    def verify(tree):  # type: ignore[no-untyped-def]
        action()
        return real_verify(tree)

    monkeypatch.setattr(fsread.ConfinedTree, "verify", verify)
    records = read_task_records(state, PROJECT_ID, "APP-1")
    monkeypatch.undo()
    return records


def _codes(records) -> set[str]:  # type: ignore[no-untyped-def]
    return {p.code for p in records.problems}


def test_q2_root_made_world_writable_after_acquisition(world, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    write_record(world["state"], intent())
    try:
        records = _read_with_change_before_verify(world["state"], monkeypatch, lambda: os.chmod(world["state"], 0o777))
    finally:
        os.chmod(world["state"], 0o755)
    assert not records.readable and _codes(records) == {"UNSAFE_PERMISSIONS"}


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS ACLs")
def test_q2_acl_added_to_root_after_acquisition(world, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    write_record(world["state"], intent())

    def add_acl() -> None:
        subprocess.run(["chmod", "+a", "everyone allow add_file", str(world["state"])], check=True)

    records = _read_with_change_before_verify(world["state"], monkeypatch, add_acl)
    assert not records.readable and _codes(records) == {"UNSAFE_PERMISSIONS"}


def test_q2_file_made_group_writable_after_it_was_read(world, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """chmod advances the file's status-change time, so the change itself is detected first."""
    path = write_record(world["state"], intent())
    records = _read_with_change_before_verify(world["state"], monkeypatch, lambda: os.chmod(path, 0o664))
    assert not records.readable and _codes(records) == {"TREE_CHANGED"}


@pytest.mark.parametrize("change", ["replaced", "renamed-away"])
def test_q2_configured_root_replaced_or_renamed_during_observation(world, monkeypatch, change: str) -> None:  # type: ignore[no-untyped-def]
    other = world["tmp"] / "other-state"
    write_record(other, intent())

    def swap() -> None:
        os.rename(world["state"], world["tmp"] / "moved-state")
        if change == "replaced":
            os.rename(other, world["state"])

    records = _read_with_change_before_verify(world["state"], monkeypatch, swap)
    assert not records.readable and _codes(records) == {"ROOT_CHANGED"}


def test_q2_root_replaced_through_the_cli_is_unknown(world, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    other = world["tmp"] / "other-state"
    write_record(other, intent())
    real_verify = fsread.ConfinedTree.verify
    done: list[bool] = []

    def verify(tree):  # type: ignore[no-untyped-def]
        if tree.root == world["state"].resolve() and not done:
            done.append(True)
            os.rename(world["state"], world["tmp"] / "moved-state")
            os.rename(other, world["state"])
        return real_verify(tree)

    monkeypatch.setattr(fsread.ConfinedTree, "verify", verify)
    code, document = _reconcile(world["project"], world["checkout"], world["state"])
    assert done and (document["state"], code) == ("UNKNOWN", 3)


def test_q2_missing_root_is_an_observation_of_absence_in_a_trusted_parent(world) -> None:  # type: ignore[no-untyped-def]
    missing = world["tmp"] / "no-state-yet"
    assert read_task_records(missing, PROJECT_ID, "APP-1").readable
    shared = world["tmp"] / "shared"
    shared.mkdir()
    os.chmod(shared, 0o777)
    try:
        records = read_task_records(shared / "no-state-yet", PROJECT_ID, "APP-1")
    finally:
        os.chmod(shared, 0o755)
    assert not records.readable  # absence observed in an untrusted directory proves nothing


# == Q3: one remote-tracking policy for reconcile and preflight ======================================


def _preflight_codes(project: Path, checkout: Path, state: Path, tmp: Path) -> tuple[str, set[str]]:
    loaded = load_project(project).project
    assert loaded is not None
    snapshot = parse_snapshot(ready_snapshot(project), loaded, "s").snapshot
    report = preflight(
        loaded, "APP-1", 3, checkout, state, make_archon_home(tmp / "archon"), evaluate(loaded, snapshot),
        api(preflight_responses())[0],
    )  # fmt: skip
    document = report.to_dict()
    return document["reconciliation"]["state"], set(document["dryRun"]["reasonCodes"])


@pytest.mark.parametrize(
    ("setup", "reconcile_state", "preflight_code"),
    [
        (
            lambda c: _remote_ref(c).write_text(fake_sha("tracking") + "\n"),
            "UNCERTAIN",
            "REMOTE_TRACKING_BRANCH_EXISTS",
        ),
        (
            lambda c: _remote_ref(c).write_text("ref: refs/remotes/origin/next\n"),
            "UNCERTAIN",
            "REMOTE_TRACKING_BRANCH_EXISTS",
        ),
        (lambda c: _remote_ref(c).write_text("garbage\n"), "UNCERTAIN", "REMOTE_TRACKING_BRANCH_EXISTS"),
        (
            lambda c: (c / ".git" / "refs" / "remotes" / "origin" / "work").symlink_to("/"),
            "UNKNOWN",
            "REMOTE_TRACKING_UNKNOWN",
        ),
        (lambda c: None, "NO_RUN", None),
    ],
    ids=["present", "symbolic", "malformed", "unknown-subtree", "absent"],
)
def test_q3_reconcile_and_preflight_agree_on_remote_tracking(
    tmp_path: Path, setup, reconcile_state: str, preflight_code: str | None
) -> None:  # type: ignore[no-untyped-def]
    project = copy_project(DISPATCH, tmp_path)
    checkout = make_checkout(tmp_path / "checkout")
    if reconcile_state == "UNKNOWN":
        (checkout / ".git" / "refs" / "remotes" / "origin").mkdir(parents=True)
    setup(checkout)
    code, document = _reconcile(project, checkout, tmp_path / "state")
    assert document["state"] == reconcile_state
    preflight_state, codes = _preflight_codes(project, checkout, tmp_path / "state", tmp_path)
    assert preflight_state == reconcile_state  # one observation policy
    tracking = {c for c in codes if "TRACKING" in c}
    assert tracking == ({preflight_code} if preflight_code else set())
    blocks = document["blocksDispatch"]
    assert blocks is (reconcile_state != "NO_RUN")
    assert (code == 3) is (reconcile_state in ("UNKNOWN", "UNCERTAIN"))


def _remote_ref(checkout: Path) -> Path:
    path = checkout / ".git" / "refs" / "remotes" / "origin" / "work" / "APP-1-r3"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def test_q3_tracking_never_replaces_live_github_state(tmp_path: Path) -> None:
    """A tracking ref that agrees with GitHub (branch exists) is not a contradiction; GitHub decides."""
    project = copy_project(DISPATCH, tmp_path)
    checkout = make_checkout(tmp_path / "checkout", remotes={R3: fake_sha("t")})
    args = [
        "reconcile",
        project,
        "--task",
        "APP-1",
        "--checkout",
        checkout,
        "--state-dir",
        tmp_path / "state",
        "--live",
    ]
    out = io.StringIO()
    responses = remote_responses(remote_branches=[R3])
    run([*map(str, args), "--format", "json"], out=out, err=io.StringIO(), transport=FakeTransport(responses))
    document = json.loads(out.getvalue())
    assert document["state"] == "UNTRACKED_BRANCH"
    assert "REMOTE_TRACKING_CONTRADICTS_REMOTE" not in {f["code"] for f in document["findings"]}


# == Q4: bounded YAML subset ====================================================================


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("a: &a [*a]\n", "YAML_UNSUPPORTED"),
        ("a: &a {b: *a}\n", "YAML_UNSUPPORTED"),
        ("a0: &a0 [x, x]\n" + "".join(f"a{i}: &a{i} [*a{i - 1}, *a{i - 1}]\n" for i in range(1, 40)), "YAML_UNSUPPORTED"),
        ("m: {<<: {k: v}}\n", "YAML_UNSUPPORTED"),
        ("m:\n  <<: {k: v}\n", "YAML_UNSUPPORTED"),
        ("a: !!str x\n", "YAML_UNSUPPORTED"),
        ("a: !!map {}\n", "YAML_UNSUPPORTED"),
        ("? [a, b]\n: c\n", "YAML_UNSUPPORTED"),
        ("? {a: b}\n: c\n", "YAML_UNSUPPORTED"),
        ("a: 1\na: 2\n", "INVALID_YAML"),
        ("a: {b: 1, 'b': 2}\n", "INVALID_YAML"),
        ("a: " + "x" * (documents.MAX_YAML_BYTES + 1) + "\n", "YAML_TOO_COMPLEX"),
        ("a: " + "1" * 5000 + "\n", "INVALID_YAML"),
    ],
    ids=[
        "recursive-sequence", "recursive-mapping", "alias-amplification", "flow-merge", "block-merge", "scalar-tag",
        "mapping-tag", "sequence-key", "mapping-key", "duplicate", "duplicate-quoted", "large-scalar", "huge-integer",
    ],
)  # fmt: skip
def test_q4_yaml_outside_the_subset_is_refused_before_composition(
    monkeypatch: pytest.MonkeyPatch, text: str, code: str
) -> None:
    composed: list[bool] = []
    real_load = yaml.load

    def loading(*args: Any, **kwargs: Any) -> Any:
        composed.append(True)
        return real_load(*args, **kwargs)

    monkeypatch.setattr(documents.yaml, "load", loading)
    started = time.monotonic()
    with pytest.raises(DocumentError) as info:
        parse_yaml(text)
    assert info.value.code == code
    assert time.monotonic() - started < 2
    if code != "INVALID_YAML" or "duplicate" in info.value.message:
        assert composed == []  # refused on the event stream, before any recursive processing


def test_q4_large_input_is_refused_before_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("the YAML parser ran on oversized input")

    monkeypatch.setattr(documents.yaml, "parse", forbidden)
    with pytest.raises(DocumentError) as info:
        parse_yaml("a: b\n" * (documents.MAX_YAML_BYTES // 4))
    assert info.value.code == "YAML_TOO_COMPLEX"


def test_q4_every_factory_core_fixture_is_inside_the_subset() -> None:
    root = Path(__file__).resolve().parents[1] / "fixtures"
    for path in sorted(root.rglob("*.yaml")):
        parse_yaml(path.read_text())


@pytest.mark.parametrize(
    "addition",
    ["x-extra: &a [*a]\n", "x-extra:\n  m: {<<: {k: v}}\n", "x-extra: " + "1" * 5000 + "\n"],
    ids=["recursive-alias", "merge", "huge-integer"],
)
def test_q4_through_validate_is_a_structured_refusal(tmp_path: Path, addition: str) -> None:
    project = copy_project(DISPATCH, tmp_path)
    task = project / "factory" / "tasks" / "APP-2.yaml"
    task.write_text(task.read_text() + addition)
    out = io.StringIO()
    assert run(["validate", str(project), "--format", "json"], out=out, err=io.StringIO()) == 2
    assert {e["code"] for e in json.loads(out.getvalue())["errors"]} & {"YAML_UNSUPPORTED", "INVALID_YAML"}


# == Q5: JSON parser failures ===================================================================


def test_q5_huge_integer_is_a_structured_document_error() -> None:
    with pytest.raises(DocumentError) as info:
        parse_json('{"attempt": ' + "1" * 5000 + "}")
    assert info.value.code == "INVALID_JSON"
    assert sys.get_int_max_str_digits() in (0, 4300) or sys.get_int_max_str_digits() > 0  # limit left as is


def test_q5_huge_integer_in_a_record_through_the_cli(world) -> None:  # type: ignore[no-untyped-def]
    path = write_record(world["state"], intent())
    path.write_text(path.read_text().replace('"attempt": 1', '"attempt": ' + "1" * 5000))
    code, document = _reconcile(world["project"], world["checkout"], world["state"])
    assert (document["state"], document["blocksDispatch"], code) == ("UNCERTAIN", True, 3)
    records = read_task_records(world["state"], PROJECT_ID, "APP-1")
    assert records.readable and "RECORD_CORRUPT" in {p.code for p in records.all_problems()}
    assert records.revision(3).attempts  # the record's slot is kept, never an empty history


def test_q5_huge_integer_in_a_snapshot_is_invalid_input(tmp_path: Path) -> None:
    snapshot = tmp_path / "s.json"
    document = ready_snapshot(DISPATCH)
    text = json.dumps(document).replace('"approvals": [', '"x": ' + "9" * 5000 + ', "approvals": [', 1)
    snapshot.write_text(text)
    assert run(["plan", str(DISPATCH), "--evidence", str(snapshot)], out=io.StringIO(), err=io.StringIO()) == 2
