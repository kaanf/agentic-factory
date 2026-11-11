"""Run-record reader: schemas, partial, corrupt and contradictory records, filesystem safety, lock probe."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path

import pytest
from stage5_support import PROJECT_ID, intent, outcome, record_dir, write_record

from factory.runs import probe_lock, read_task_records


def _problems(state: Path, task: str = "APP-1") -> set[str]:
    return {p.code for p in read_task_records(state, PROJECT_ID, task).all_problems()}


def _fingerprint(root: Path) -> dict[str, tuple[str, int]]:
    return {
        str(p.relative_to(root)): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
        for p in sorted(root.rglob("*"))
        if p.is_file() and not p.is_symlink()
    }


def test_missing_state_directory_means_no_records(tmp_path: Path) -> None:
    records = read_task_records(tmp_path / "nothing", PROJECT_ID, "APP-1")
    assert records.readable and records.revisions == {} and records.problems == ()


def test_complete_attempts_are_read(tmp_path: Path) -> None:
    write_record(tmp_path, intent(attempt=1))
    write_record(tmp_path, outcome(attempt=1, exit_code=1))
    write_record(tmp_path, intent(attempt=2))
    write_record(tmp_path, outcome(attempt=2, status="abandoned", exit_code=None))
    records = read_task_records(tmp_path, PROJECT_ID, "APP-1")
    revision = records.revision(3)
    assert [a.to_dict() for a in revision.attempts] == [
        {"attempt": 1, "exitCode": 1, "intent": True, "outcome": "exited"},
        {"attempt": 2, "exitCode": None, "intent": True, "outcome": "abandoned"},
    ]
    assert records.all_problems() == []


def test_intent_without_outcome_is_an_interrupted_attempt(tmp_path: Path) -> None:
    write_record(tmp_path, intent())
    last = read_task_records(tmp_path, PROJECT_ID, "APP-1").revision(3).last
    assert last is not None and last.interrupted


@pytest.mark.parametrize(
    ("content", "code"),
    [
        (b"", "RECORD_CORRUPT"),
        (b'{"schemaVersion": "factory.run-intent/v1"', "RECORD_CORRUPT"),
        (b"\xff\xfe", "RECORD_CORRUPT"),
        (b'{"a": 1, "a": 2}', "RECORD_CORRUPT"),
        (b'{"x": NaN}', "RECORD_CORRUPT"),
        (json.dumps({"schemaVersion": "factory.run-intent/v1"}).encode(), "RECORD_SCHEMA_INVALID"),
        (json.dumps(intent(extra="field")).encode(), "RECORD_SCHEMA_INVALID"),
        (json.dumps(intent(createdAt="yesterday")).encode(), "RECORD_SCHEMA_INVALID"),
        (b"x" * (64 * 1024 + 1), "RECORD_TOO_LARGE"),
    ],
)
def test_corrupt_records_are_reported(tmp_path: Path, content: bytes, code: str) -> None:
    (record_dir(tmp_path) / "attempt-1.intent.json").write_bytes(content)
    assert code in _problems(tmp_path)


def test_exited_outcome_needs_an_exit_code(tmp_path: Path) -> None:
    write_record(tmp_path, intent())
    write_record(tmp_path, outcome(exit_code=None))
    assert "RECORD_SCHEMA_INVALID" in _problems(tmp_path)


@pytest.mark.parametrize(
    "changes",
    [{"task": "APP-2"}, {"revision": 2}, {"attempt": 2}, {"project": "another-project"}],
)
def test_records_that_contradict_their_location(tmp_path: Path, changes: dict[str, object]) -> None:
    record = intent()
    path = write_record(tmp_path, record)
    path.write_text(json.dumps({**record, **changes}))
    assert "RECORD_CONTRADICTS_PATH" in _problems(tmp_path)


def test_outcome_without_intent_and_attempt_gaps(tmp_path: Path) -> None:
    write_record(tmp_path, outcome(attempt=1))
    write_record(tmp_path, intent(attempt=3))
    write_record(tmp_path, outcome(attempt=3))
    assert {"OUTCOME_WITHOUT_INTENT", "ATTEMPT_GAP"} <= _problems(tmp_path)


def test_interruption_followed_by_a_later_attempt_is_contradictory(tmp_path: Path) -> None:
    write_record(tmp_path, intent(attempt=1))
    write_record(tmp_path, intent(attempt=2))
    write_record(tmp_path, outcome(attempt=2))
    assert "LATER_ATTEMPT_AFTER_INTERRUPTION" in _problems(tmp_path)


def test_unexpected_and_temporary_entries(tmp_path: Path) -> None:
    write_record(tmp_path, intent())
    directory = record_dir(tmp_path)
    (directory / ".attempt-1.outcome.json.tmp").write_text("{}")
    (directory / "notes.txt").write_text("x")
    (directory / "attempt-01.intent.json").write_text("{}")
    (tmp_path / PROJECT_ID / "runs" / "APP-1" / "latest").write_text("x")
    (tmp_path / PROJECT_ID / "runs" / "APP-1" / "r0").mkdir()
    assert {"TEMPORARY_FILE_PRESENT", "UNEXPECTED_ENTRY"} <= _problems(tmp_path)


def test_symbolic_links_are_not_followed(tmp_path: Path) -> None:
    state = tmp_path / "state"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "attempt-1.intent.json").write_text(json.dumps(intent()))
    directory = record_dir(state)
    (directory / "attempt-1.intent.json").symlink_to(outside / "attempt-1.intent.json")
    assert "RECORD_SYMLINK" in _problems(state)

    # A revision directory that is a link to somewhere else is not entered.
    state2 = tmp_path / "state2"
    runs = state2 / PROJECT_ID / "runs" / "APP-1"
    runs.mkdir(parents=True)
    (runs / "r3").symlink_to(outside)
    records = read_task_records(state2, PROJECT_ID, "APP-1")
    assert 3 not in records.revisions
    assert "RECORD_SYMLINK" in {p.code for p in records.all_problems()}

    # So is a project or task directory link.
    state3 = tmp_path / "state3"
    state3.mkdir()
    (state3 / PROJECT_ID).symlink_to(state / PROJECT_ID)
    records = read_task_records(state3, PROJECT_ID, "APP-1")
    assert not records.readable


def test_hard_linked_records_are_not_trusted(tmp_path: Path) -> None:
    path = write_record(tmp_path, intent())
    os.link(path, tmp_path / "elsewhere.json")
    assert "RECORD_HARD_LINKED" in _problems(tmp_path)


@pytest.mark.parametrize("task", ["../APP-1", "APP-1/../../x", "APP-1\n", ".."])
def test_unsafe_task_ids_never_reach_the_filesystem(tmp_path: Path, task: str) -> None:
    records = read_task_records(tmp_path, PROJECT_ID, task)
    assert not records.readable
    assert {p.code for p in records.problems} == {"RECORD_PATH_UNSAFE"}


def test_unsafe_project_id(tmp_path: Path) -> None:
    assert not read_task_records(tmp_path, "../x", "APP-1").readable


def test_reader_never_writes(tmp_path: Path) -> None:
    write_record(tmp_path, intent(attempt=1))
    write_record(tmp_path, outcome(attempt=1))
    write_record(tmp_path, intent(attempt=2))
    (tmp_path / PROJECT_ID / "dispatch.lock").write_text("")
    before = _fingerprint(tmp_path)
    entries_before = sorted(str(p) for p in tmp_path.rglob("*"))
    read_task_records(tmp_path, PROJECT_ID, "APP-1")
    probe_lock(tmp_path, PROJECT_ID)
    assert _fingerprint(tmp_path) == before
    assert sorted(str(p) for p in tmp_path.rglob("*")) == entries_before


def test_reader_does_not_create_the_state_directory(tmp_path: Path) -> None:
    read_task_records(tmp_path / "state", PROJECT_ID, "APP-1")
    assert probe_lock(tmp_path / "state", PROJECT_ID) == "absent"
    assert not (tmp_path / "state").exists()


# -- lock ------------------------------------------------------------------------------------------


def test_lock_probe(tmp_path: Path) -> None:
    assert probe_lock(tmp_path, PROJECT_ID) == "absent"
    lock = tmp_path / PROJECT_ID / "dispatch.lock"
    lock.parent.mkdir()
    lock.write_text("")
    assert probe_lock(tmp_path, PROJECT_ID) == "free"
    fd = os.open(lock, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert probe_lock(tmp_path, PROJECT_ID) == "held"
    finally:
        os.close(fd)
    assert probe_lock(tmp_path, PROJECT_ID) == "free"


def test_lock_that_is_a_link_or_directory_is_unknown(tmp_path: Path) -> None:
    (tmp_path / PROJECT_ID).mkdir()
    (tmp_path / "real.lock").write_text("")
    (tmp_path / PROJECT_ID / "dispatch.lock").symlink_to(tmp_path / "real.lock")
    assert probe_lock(tmp_path, PROJECT_ID) == "unknown"
    (tmp_path / PROJECT_ID / "dispatch.lock").unlink()
    (tmp_path / PROJECT_ID / "dispatch.lock").mkdir()
    assert probe_lock(tmp_path, PROJECT_ID) == "unknown"
