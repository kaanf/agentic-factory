"""Evidence output (`plan --live --save-evidence`, `evidence collect --output`) never writes into the project.

Each scenario runs against a temporary copy of the self-describing fixture, with recorded GitHub
responses. Attacks that happen "during collection" run from inside the first GitHub request. Results
are checked on inodes and bytes, not only on path names.
"""

from __future__ import annotations

import errno
import hashlib
import io
import json
import os
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from github_support import SELF_PROJECT, load_recorded

from factory import output as output_module
from factory.cli import run
from factory.evidence import parse_snapshot
from factory.github import Response
from factory.project import load_project
from factory.render import to_json

PROFILE = "factory/profiles/factory.yaml"
COMMANDS = ["plan", "collect"]


class DuringCollection:
    """Recorded responses; ``action`` runs once, at the first request (after the destination was checked)."""

    def __init__(self, action: Callable[[], None]) -> None:
        self.inner = load_recorded()
        self.action: Callable[[], None] | None = action
        self.requests = self.inner.requests

    def get(self, path: str) -> Response:
        if self.action is not None:
            action, self.action = self.action, None
            action()
        return self.inner.get(path)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    return Path(shutil.copytree(SELF_PROJECT, tmp_path / "project"))


@pytest.fixture
def out(tmp_path: Path) -> Path:
    directory = tmp_path / "out"
    directory.mkdir()
    return directory


def fingerprint(root: Path) -> dict[str, tuple[str, int, int]]:
    """Every entry under ``root``: content hash (or link target), inode and modification time."""
    entries = {}
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        content = (
            f"link:{os.readlink(path)}"
            if path.is_symlink()
            else hashlib.sha256(path.read_bytes()).hexdigest()
            if path.is_file()
            else "dir"
        )
        entries[path.relative_to(root).as_posix()] = (content, info.st_ino, info.st_mtime_ns)
    return entries


def save(command: str, project: Path, destination: Path | str, transport: Any = None) -> tuple[int, str, str]:
    args = (
        ["plan", project, "--live", "--format", "json", "--save-evidence", destination]
        if command == "plan"
        else ["evidence", "collect", project, "--output", destination]
    )
    stdout, stderr = io.StringIO(), io.StringIO()
    code = run([str(a) for a in args], out=stdout, err=stderr, transport=transport or load_recorded())
    return code, stdout.getvalue(), stderr.getvalue()


def assert_snapshot(path: Path) -> dict[str, Any]:
    """The file is one complete, canonical, schema-valid snapshot for the project."""
    text = path.read_text()
    document: dict[str, Any] = json.loads(text)
    assert to_json(document) == text
    loaded = load_project(SELF_PROJECT).project
    assert loaded is not None
    assert parse_snapshot(document, loaded, path.as_posix()).errors == []
    return document


def no_temporary_files(directory: Path) -> bool:
    return not [p for p in directory.iterdir() if p.name.startswith(".factory-")]


# -- A, B, R, S, I, J: normal saves -----------------------------------------------------------


@pytest.mark.parametrize("command", COMMANDS)
def test_save_to_a_new_external_file(project: Path, out: Path, command: str) -> None:
    before = fingerprint(project)
    code, _, _ = save(command, project, out / "snapshot.json")
    assert code == 0
    assert_snapshot(out / "snapshot.json")
    assert sorted(p.name for p in out.iterdir()) == ["snapshot.json"]
    assert fingerprint(project) == before


@pytest.mark.parametrize("command", COMMANDS)
def test_existing_external_file_is_replaced_by_a_new_entry(project: Path, out: Path, command: str) -> None:
    destination, other = out / "snapshot.json", out / "other-link.json"
    destination.write_text("old snapshot\n")
    os.link(destination, other)
    old_inode = destination.stat().st_ino
    assert save(command, project, destination)[0] == 0
    assert destination.stat().st_ino != old_inode
    assert_snapshot(destination)
    # The old file was never rewritten: its other hard link still has the old contents.
    assert other.read_text() == "old snapshot\n" and other.stat().st_ino == old_inode


def test_saved_evidence_replans_to_the_live_plan(project: Path, out: Path) -> None:
    code, live_plan, _ = save("plan", project, out / "snapshot.json")
    assert code == 0
    stdout, stderr = io.StringIO(), io.StringIO()
    args = ["plan", str(project), "--evidence", str(out / "snapshot.json"), "--format", "json"]
    assert run(args, out=stdout, err=stderr) == 0
    assert stdout.getvalue() == live_plan


@pytest.mark.parametrize("command", COMMANDS)
def test_relative_destination(project: Path, out: Path, command: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(out.parent)
    assert save(command, project, "out/snapshot.json")[0] == 0
    assert_snapshot(out / "snapshot.json")


@pytest.mark.parametrize("command", COMMANDS)
def test_relative_destination_resolving_into_the_project_is_refused(
    project: Path, command: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(project / "factory")
    before = fingerprint(project)
    code, _, err = save(command, project, "snapshot.json")
    assert code == 64 and "must not be inside the project directory" in err
    assert fingerprint(project) == before


# -- C, D, G, K, L, T: destinations that alias the project ------------------------------------


@pytest.mark.parametrize("command", COMMANDS)
def test_external_hard_link_to_a_project_profile(project: Path, out: Path, command: str) -> None:
    profile = project / PROFILE
    destination = out / "snapshot.json"
    os.link(profile, destination)
    before = fingerprint(project)
    assert save(command, project, destination)[0] == 0
    assert fingerprint(project) == before
    assert profile.read_text().startswith("schemaVersion: factory.profile/v1")
    assert destination.stat().st_ino != profile.stat().st_ino
    assert_snapshot(destination)


@pytest.mark.parametrize("command", COMMANDS)
@pytest.mark.parametrize("inside", [True, False], ids=["into-project", "elsewhere"])
def test_existing_symlink_destination_is_refused(
    project: Path, out: Path, tmp_path: Path, command: str, inside: bool
) -> None:
    target = project / PROFILE if inside else tmp_path / "elsewhere.json"
    if not inside:
        target.write_text("unrelated\n")
    target_bytes = target.read_bytes()
    destination = out / "snapshot.json"
    destination.symlink_to(target)
    before = fingerprint(project)
    transport = load_recorded()
    code, _, err = save(command, project, destination, transport)
    assert code == 64
    assert "is a symbolic link; refusing to write through or replace it" in err
    assert transport.requests == []
    assert target.read_bytes() == target_bytes and os.readlink(destination) == str(target)
    assert fingerprint(project) == before


@pytest.mark.parametrize("command", COMMANDS)
def test_symlinked_parent_pointing_into_the_project_is_refused(project: Path, tmp_path: Path, command: str) -> None:
    (tmp_path / "linked-out").symlink_to(project / "factory")
    before = fingerprint(project)
    code, _, err = save(command, project, tmp_path / "linked-out" / "snapshot.json")
    assert code == 64 and "must not be inside the project directory" in err
    assert fingerprint(project) == before


@pytest.mark.parametrize("command", COMMANDS)
@pytest.mark.parametrize(
    "relative",
    [
        pytest.param("snapshot.json", id="new-file"),
        pytest.param(PROFILE, id="existing-profile"),
        pytest.param("../project/factory/snapshot.json", id="dot-dot-into-project"),
    ],
)
def test_destination_inside_the_project_is_refused(project: Path, out: Path, command: str, relative: str) -> None:
    destination = (out / relative) if relative.startswith("..") else project / relative
    before = fingerprint(project)
    transport = load_recorded()
    code, _, err = save(command, project, destination, transport)
    assert code == 64 and "must not be inside the project directory" in err
    assert transport.requests == []
    assert fingerprint(project) == before


@pytest.mark.parametrize("command", COMMANDS)
def test_directory_destination_is_refused(project: Path, out: Path, command: str) -> None:
    (out / "snapshot.json").mkdir()
    code, _, err = save(command, project, out / "snapshot.json")
    assert code == 64 and "not a regular file" in err


# -- E, F, H: the path changes while evidence is being collected -------------------------------


@pytest.mark.parametrize("command", COMMANDS)
@pytest.mark.parametrize("existing", [False, True], ids=["absent-destination", "existing-destination"])
def test_destination_replaced_by_a_symlink_during_collection(
    project: Path, out: Path, command: str, existing: bool
) -> None:
    profile = project / PROFILE
    destination = out / "snapshot.json"
    if existing:
        destination.write_text("old snapshot\n")
    before = fingerprint(project)

    def swap() -> None:
        destination.unlink(missing_ok=True)
        destination.symlink_to(profile)

    assert save(command, project, destination, DuringCollection(swap))[0] == 0
    assert fingerprint(project) == before
    # The symbolic link was replaced, not followed.
    assert not destination.is_symlink()
    assert_snapshot(destination)
    assert no_temporary_files(out)


@pytest.mark.parametrize("command", COMMANDS)
def test_destination_hard_linked_to_the_project_during_collection(project: Path, out: Path, command: str) -> None:
    destination = out / "snapshot.json"
    before = fingerprint(project)
    assert (
        save(command, project, destination, DuringCollection(lambda: os.link(project / PROFILE, destination)))[0] == 0
    )
    assert fingerprint(project) == before
    assert_snapshot(destination)


def _parent_to_symlink(out: Path, project: Path) -> None:
    out.rename(out.with_name("out-moved"))
    out.symlink_to(project / "factory")


def _parent_to_new_directory(out: Path, project: Path) -> None:
    out.rename(out.with_name("out-moved"))
    out.mkdir()


def _parent_into_project(out: Path, project: Path) -> None:
    out.rename(project / "factory" / "moved-out")


@pytest.mark.parametrize("command", COMMANDS)
@pytest.mark.parametrize(
    "change",
    [
        pytest.param(_parent_to_symlink, id="parent-replaced-by-symlink-into-project"),
        pytest.param(_parent_to_new_directory, id="parent-replaced-by-another-directory"),
        pytest.param(_parent_into_project, id="parent-moved-into-project"),
    ],
)
def test_parent_directory_changed_during_collection_saves_nothing(
    project: Path, out: Path, command: str, change: Callable[[Path, Path], None]
) -> None:
    before = fingerprint(project)
    code, stdout, err = save(command, project, out / "snapshot.json", DuringCollection(lambda: change(out, project)))
    assert code == 73
    assert "was moved or replaced; nothing was saved" in err
    assert stdout == ""
    after = fingerprint(project)
    if change is _parent_into_project:
        # The moved directory itself is the only new entry, and it is still empty. Moving it in
        # updated the modification time of its new parent; nothing else changed.
        assert after.pop("factory")[:2] == before.pop("factory")[:2]
        assert {k: v for k, v in after.items() if not k.startswith("factory/moved-out")} == before
        assert list((project / "factory" / "moved-out").iterdir()) == []
    else:
        assert after == before
    for directory in (out.with_name("out-moved"), out):
        if directory.is_dir() and not directory.is_symlink():
            assert list(directory.iterdir()) == []


# -- M, N, O, P, Q: failures leave the destination as it was -----------------------------------


@pytest.fixture
def existing(out: Path) -> Path:
    destination = out / "snapshot.json"
    destination.write_text("previous snapshot\n")
    return destination


def assert_untouched(destination: Path, inode: int) -> None:
    assert destination.read_text() == "previous snapshot\n"
    assert destination.stat().st_ino == inode
    assert no_temporary_files(destination.parent)


@pytest.mark.parametrize("command", COMMANDS)
def test_serialization_failure(project: Path, existing: Path, command: str, monkeypatch: pytest.MonkeyPatch) -> None:
    inode = existing.stat().st_ino

    def unserializable(_document: Any) -> str:
        raise TypeError("Object of type bytes is not JSON serializable")

    monkeypatch.setattr(output_module, "to_json", unserializable)
    code, stdout, err = save(command, project, existing)
    assert (code, stdout) == (73, "")
    assert "cannot serialize the snapshot" in err
    assert_untouched(existing, inode)


@pytest.mark.skipif(os.geteuid() == 0, reason="permissions do not apply to root")
@pytest.mark.parametrize("command", COMMANDS)
def test_permission_failure(project: Path, existing: Path, command: str) -> None:
    inode = existing.stat().st_ino
    existing.parent.chmod(0o500)
    try:
        code, stdout, err = save(command, project, existing)
    finally:
        existing.parent.chmod(0o700)
    assert (code, stdout) == (73, "")
    assert "Permission denied" in err
    assert_untouched(existing, inode)


def _partial_then(exc: BaseException) -> Callable[[int, bytes], None]:
    def write(fd: int, data: bytes) -> None:
        os.write(fd, data[: len(data) // 2])
        raise exc

    return write


@pytest.mark.parametrize("command", COMMANDS)
def test_disk_full_during_write(project: Path, existing: Path, command: str, monkeypatch: pytest.MonkeyPatch) -> None:
    inode = existing.stat().st_ino
    monkeypatch.setattr(output_module, "_write_all", _partial_then(OSError(errno.ENOSPC, "No space left on device")))
    code, stdout, err = save(command, project, existing)
    assert (code, stdout) == (73, "")
    assert "No space left on device" in err
    assert_untouched(existing, inode)


@pytest.mark.parametrize("command", COMMANDS)
def test_interrupted_write(project: Path, existing: Path, command: str, monkeypatch: pytest.MonkeyPatch) -> None:
    inode = existing.stat().st_ino
    monkeypatch.setattr(output_module, "_write_all", _partial_then(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        save(command, project, existing)
    assert_untouched(existing, inode)


@pytest.mark.parametrize("command", COMMANDS)
def test_atomic_replacement_failure(
    project: Path, existing: Path, command: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    inode = existing.stat().st_ino
    real_replace = os.replace

    def failing_replace(src: Any, dst: Any, **kwargs: Any) -> None:
        if "src_dir_fd" in kwargs:
            raise OSError(errno.EIO, "Input/output error")
        real_replace(src, dst, **kwargs)

    monkeypatch.setattr(os, "replace", failing_replace)
    code, stdout, err = save(command, project, existing)
    assert (code, stdout) == (73, "")
    assert "Input/output error" in err
    assert_untouched(existing, inode)


def test_short_writes_are_completed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real_write = os.write
    monkeypatch.setattr(os, "write", lambda fd, data: real_write(fd, bytes(data[:3])))
    fd = os.open(tmp_path / "f", os.O_WRONLY | os.O_CREAT)
    try:
        output_module._write_all(fd, b"0123456789")
    finally:
        os.close(fd)
    assert (tmp_path / "f").read_bytes() == b"0123456789"


@pytest.mark.parametrize("command", COMMANDS)
def test_missing_destination_directory_fails_before_collection(project: Path, out: Path, command: str) -> None:
    transport = load_recorded()
    code, _, err = save(command, project, out / "missing" / "snapshot.json", transport)
    assert code == 73 and "is not usable" in err
    assert transport.requests == []


@pytest.mark.parametrize("command", COMMANDS)
def test_platform_without_directory_relative_operations_is_refused(
    project: Path, out: Path, command: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "supports_dir_fd", set())
    transport = load_recorded()
    code, _, err = save(command, project, out / "snapshot.json", transport)
    assert code == 73 and "cannot be saved safely" in err
    assert transport.requests == [] and list(out.iterdir()) == []
