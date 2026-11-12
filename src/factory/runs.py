"""Run-record reader for the Stage 6 dispatch ledger. Read-only: Stage 5 never writes run records.

Layout under the runtime state directory (default ``~/.factory``)::

    <project>/dispatch.lock
    <project>/runs/<task-id>/r<revision>/attempt-<N>.intent.json
    <project>/runs/<task-id>/r<revision>/attempt-<N>.outcome.json

The guarantees a ledger needs (one exclusive lock, intent created with ``O_CREAT|O_EXCL`` and fsynced
before Archon starts, outcome written to a temporary file and renamed) belong to the Stage 6 writer,
which does not exist yet. This reader assumes none of them. It reports whatever it finds:

- Every path component is validated before use, and every read goes through
  ``fsread.ConfinedTree``: symbolic links are refused, record files must be regular, single-link files
  of bounded size.
- Each record is parsed strictly (duplicate keys rejected), validated against its schema, and
  compared with its location: a record whose project, task, revision or attempt differs from its
  path is contradictory.
- Missing intents, gaps in attempt numbers, leftover temporary files, unknown entries and records
  from another host are reported as problems. Callers treat any problem as UNCERTAIN, never as
  success.
"""

from __future__ import annotations

import errno
import fcntl
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from factory.documents import DocumentError, parse_json
from factory.fsread import INTEGRITY_CODES, ConfinedTree, FsError
from factory.refs import is_slug, is_task_id
from factory.schemas.validation import validate_document

RECORD_BYTES = 64 * 1024
# Budgets for one task's records, enforced while reading. A revision holds at most
# 2 x maxAttemptsPerTaskRevision (<= 10, schema maximum) record files, so 10,000 observed objects
# cover more than 400 revisions at the attempt cap plus stray entries; 16 MiB is 256 records at
# RECORD_BYTES (real records are well under 1 KiB). Exceeding either makes the records unreadable
# (BUDGET_EXCEEDED), never an empty or truncated history.
MAX_RECORD_OBJECTS = 10_000
MAX_RECORD_BYTES = 16 * 1024 * 1024
LOCK_FILE = "dispatch.lock"
_ATTEMPT = re.compile(r"attempt-([1-9][0-9]{0,5})\.(intent|outcome)\.json")
_REVISION = re.compile(r"r([1-9][0-9]{0,8})")
_TEMPORARY = re.compile(r"\..*\.tmp|.*\.tmp")


@dataclass(frozen=True, order=True)
class Problem:
    code: str
    revision: int
    attempt: int
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {"attempt": self.attempt, "code": self.code, "message": self.message, "revision": self.revision}


@dataclass(frozen=True)
class Attempt:
    number: int
    intent: dict[str, Any] | None
    outcome: dict[str, Any] | None

    @property
    def interrupted(self) -> bool:
        return self.intent is not None and self.outcome is None

    def to_dict(self) -> dict[str, Any]:
        outcome = self.outcome
        return {
            "attempt": self.number,
            "exitCode": outcome.get("exitCode") if outcome else None,
            "intent": self.intent is not None,
            "outcome": outcome.get("status") if outcome else None,
        }


@dataclass(frozen=True)
class RevisionRecords:
    revision: int
    attempts: tuple[Attempt, ...]
    problems: tuple[Problem, ...] = ()

    @property
    def last(self) -> Attempt | None:
        return self.attempts[-1] if self.attempts else None


@dataclass(frozen=True)
class TaskRecords:
    """Records of one task. ``readable`` is False when the state directory could not be read at all."""

    readable: bool
    revisions: dict[int, RevisionRecords] = field(default_factory=dict)
    problems: tuple[Problem, ...] = ()

    def revision(self, number: int) -> RevisionRecords:
        return self.revisions.get(number, RevisionRecords(number, ()))

    def all_problems(self) -> list[Problem]:
        return sorted([*self.problems, *(p for r in self.revisions.values() for p in r.problems)])


def _parse(tree: ConfinedTree, parts: tuple[str, ...], kind: str) -> tuple[dict[str, Any] | None, str, str]:
    """(record, problem code, message). The record is None exactly when there is a problem."""
    try:
        data = tree.read(*parts, max_bytes=RECORD_BYTES, single_link=True)
        document = parse_json(data.decode("utf-8"))
    except FsError as exc:
        if exc.code in INTEGRITY_CODES:
            raise  # the observation as a whole is unusable, not just this record
        code = {"SYMLINK": "RECORD_SYMLINK", "HARD_LINKED": "RECORD_HARD_LINKED", "TOO_LARGE": "RECORD_TOO_LARGE"}
        return None, code.get(exc.code, "RECORD_UNREADABLE"), exc.message
    except UnicodeDecodeError:
        return None, "RECORD_CORRUPT", f"{parts[-1]} is not UTF-8"
    except DocumentError as exc:
        return None, "RECORD_CORRUPT", f"{parts[-1]}: {exc.message}"
    issues = validate_document(kind, document, parts[-1])
    if issues:
        return None, "RECORD_SCHEMA_INVALID", "; ".join(issue.render() for issue in issues[:5])
    return document, "", ""


def _read_revision(
    tree: ConfinedTree, base: tuple[str, ...], project: str, task: str, revision: int
) -> RevisionRecords:
    problems: list[Problem] = []
    files: dict[int, dict[str, dict[str, Any] | None]] = {}
    try:
        entries = tree.list(*base)
    except FsError as exc:
        if exc.code in INTEGRITY_CODES:
            raise
        return RevisionRecords(revision, (), (Problem("RECORDS_UNREADABLE", revision, 0, exc.message),))
    for entry in entries:
        match = _ATTEMPT.fullmatch(entry.name)
        if match is None:
            code = "TEMPORARY_FILE_PRESENT" if _TEMPORARY.fullmatch(entry.name) else "UNEXPECTED_ENTRY"
            problems.append(Problem(code, revision, 0, f"unexpected entry {entry.name!r} ({entry.kind})"))
            continue
        number, kind = int(match.group(1)), match.group(2)
        record, code, message = _parse(tree, (*base, entry.name), f"run-{kind}")
        if record is None:
            problems.append(Problem(code, revision, number, message))
            files.setdefault(number, {})[kind] = None
            continue
        expected = {"project": project, "task": task, "revision": revision, "attempt": number}
        mismatched = sorted(k for k, v in expected.items() if record.get(k) != v)
        if mismatched:
            problems.append(
                Problem(
                    "RECORD_CONTRADICTS_PATH",
                    revision,
                    number,
                    f"{entry.name}: {', '.join(mismatched)} differ from the record's location",
                )
            )
            files.setdefault(number, {})[kind] = None
            continue
        files.setdefault(number, {})[kind] = record

    attempts = []
    for number in sorted(files):
        kinds = files[number]
        intent, outcome = kinds.get("intent"), kinds.get("outcome")
        if "outcome" in kinds and "intent" not in kinds:
            problems.append(Problem("OUTCOME_WITHOUT_INTENT", revision, number, f"attempt {number} has no intent"))
        attempts.append(Attempt(number, intent, outcome))
    numbers = sorted(files)
    if numbers and numbers != list(range(1, numbers[-1] + 1)):
        problems.append(Problem("ATTEMPT_GAP", revision, 0, f"attempt numbers are not consecutive: {numbers}"))
    for attempt in attempts[:-1]:
        if attempt.interrupted:
            problems.append(
                Problem(
                    "LATER_ATTEMPT_AFTER_INTERRUPTION",
                    revision,
                    attempt.number,
                    f"attempt {attempt.number} has no outcome, but a later attempt exists",
                )
            )
    return RevisionRecords(revision, tuple(attempts), tuple(sorted(problems)))


def read_task_records(state_root: Path, project: str, task: str) -> TaskRecords:
    """Read every revision's records for one task as one coherent observation.

    A missing state root or task directory means no records, but only if that absence still holds
    when the read ends. Any integrity failure (an observed object replaced, a listing that changed
    while it was enumerated, unsafe permissions, an exceeded budget) makes the whole result
    unreadable: it never yields an empty or partial history.
    """
    if not is_slug(project) or not is_task_id(task):
        return TaskRecords(False, problems=(Problem("RECORD_PATH_UNSAFE", 0, 0, "project or task id is unsafe"),))
    try:
        tree = ConfinedTree.open(state_root, max_entries=MAX_RECORD_OBJECTS, max_bytes=MAX_RECORD_BYTES)
    except FsError as exc:
        if exc.code == "NOT_FOUND":
            return TaskRecords(True)
        return TaskRecords(False, problems=(Problem("STATE_UNREADABLE", 0, 0, exc.message),))
    with tree:
        try:
            result = _read_task(tree, project, task)
            tree.verify()
        except FsError as exc:
            code = exc.code if exc.code in INTEGRITY_CODES else "STATE_UNREADABLE"
            return TaskRecords(False, problems=(Problem(code, 0, 0, exc.message),))
        return result


def _read_task(tree: ConfinedTree, project: str, task: str) -> TaskRecords:
    base = (project, "runs", task)
    try:
        entries = tree.list(*base)
    except FsError as exc:
        if exc.code == "NOT_FOUND":
            return TaskRecords(True)
        raise
    problems: list[Problem] = []
    revisions: dict[int, RevisionRecords] = {}
    for entry in entries:
        match = _REVISION.fullmatch(entry.name)
        if match is None or entry.kind != "directory":
            code = "RECORD_SYMLINK" if entry.kind == "symlink" else "UNEXPECTED_ENTRY"
            problems.append(Problem(code, 0, 0, f"unexpected entry {entry.name!r} ({entry.kind}) in {task}"))
            continue
        number = int(match.group(1))
        revisions[number] = _read_revision(tree, (*base, entry.name), project, task, number)
    return TaskRecords(True, dict(sorted(revisions.items())), tuple(sorted(problems)))


def probe_lock(state_root: Path, project: str) -> str:
    """'absent', 'free', 'held' or 'unknown' for the project's dispatch lock. Changes no file.

    Takes a shared, non-blocking ``flock`` and releases it at once. A coordinator holding the
    exclusive lock makes it fail. Trying at the same instant as a coordinator can make that
    coordinator's own attempt fail, which refuses its dispatch: the safe direction.
    """
    if not is_slug(project):
        return "unknown"
    try:
        tree = ConfinedTree.open(state_root)
    except FsError as exc:
        return "absent" if exc.code == "NOT_FOUND" else "unknown"
    with tree:
        try:
            kind = tree.kind(project, LOCK_FILE)
        except FsError as exc:
            return "absent" if exc.code == "NOT_FOUND" else "unknown"
        if kind is None:
            return "absent"
        if kind != "file":
            return "unknown"
        try:
            fd = tree.open_file(project, LOCK_FILE)
        except FsError:
            return "unknown"
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError as exc:
            return "held" if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN) else "unknown"
        fcntl.flock(fd, fcntl.LOCK_UN)
        return "free"
    finally:
        os.close(fd)
