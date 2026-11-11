"""F3 regression: replacing directories while a confined root is acquired or read never escapes it.

Each test builds a trusted tree (marker ``INSIDE``) and an attacker tree (marker ``OUTSIDE``), then
replaces a directory at a precise point of the acquisition by intercepting ``os.open`` inside
``factory.fsread``. Real filesystem operations only; no mocked file contents. The reader must either
read the trusted tree or refuse, and must never return ``OUTSIDE``.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import pytest

from factory import fsread
from factory.fsread import ConfinedTree, FsError


@pytest.fixture
def trees(tmp_path: Path) -> dict[str, Path]:
    trusted = tmp_path / "a" / "b" / "trusted"
    trusted.mkdir(parents=True)
    (trusted / "m").write_text("INSIDE")
    (trusted / "sub").mkdir()
    (trusted / "sub" / "m").write_text("INSIDE")
    evil = tmp_path / "evil"
    (evil / "b" / "trusted" / "sub").mkdir(parents=True)
    (evil / "b" / "trusted" / "m").write_text("OUTSIDE")
    (evil / "b" / "trusted" / "sub" / "m").write_text("OUTSIDE")
    return {"tmp": tmp_path, "trusted": trusted, "evil": evil}


def _intercept(monkeypatch: pytest.MonkeyPatch, after: str, action: Callable[[], None]) -> list[str]:
    """Run ``action`` once, right after fsread opens a directory component named ``after``."""
    real_open = os.open
    seen: list[str] = []

    def opening(path, flags, *args, **kwargs):  # type: ignore[no-untyped-def]
        fd = real_open(path, flags, *args, **kwargs)
        seen.append(str(path))
        if str(path) == after and len([s for s in seen if s == after]) == 1:
            action()
        return fd

    monkeypatch.setattr(fsread.os, "open", opening)
    return seen


def _read(path: Path, *parts: str) -> str:
    try:
        with ConfinedTree.open(path) as tree:
            return tree.read(*parts, max_bytes=100).decode()
    except FsError as exc:
        return f"refused:{exc.code}"


def test_ancestor_replaced_by_symlink_after_it_was_opened(trees, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    tmp = trees["tmp"]

    def swap() -> None:
        os.rename(tmp / "a", tmp / "a-moved")
        (tmp / "a").symlink_to(trees["evil"])

    seen = _intercept(monkeypatch, "a", swap)
    assert _read(trees["trusted"], "m") == "INSIDE"
    assert "a" in seen  # the swap really happened mid-acquisition
    assert (tmp / "a").is_symlink()


def test_parent_replaced_by_real_directory_after_it_was_opened(trees, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    tmp = trees["tmp"]

    def swap() -> None:
        os.rename(tmp / "a" / "b", tmp / "b-moved")
        os.rename(trees["evil"] / "b", tmp / "a" / "b")

    _intercept(monkeypatch, "b", swap)
    assert _read(trees["trusted"], "m") == "INSIDE"
    assert (tmp / "b-moved").is_dir()  # the swap really happened mid-acquisition


def test_root_replaced_after_it_was_opened(trees, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    trusted = trees["trusted"]

    def swap() -> None:
        os.rename(trusted, trusted.parent / "trusted-moved")
        (trusted.parent / "trusted").symlink_to(trees["evil"] / "b" / "trusted")

    _intercept(monkeypatch, "trusted", swap)
    assert _read(trusted, "m") == "INSIDE"
    assert (trusted.parent / "trusted").is_symlink()


def test_root_identity_change_after_acquisition_does_not_change_the_tree(trees) -> None:  # type: ignore[no-untyped-def]
    trusted = trees["trusted"]
    with ConfinedTree.open(trusted) as tree:
        os.rename(trusted, trusted.parent / "gone")
        os.rename(trees["evil"] / "b" / "trusted", trusted)
        assert tree.read("m", max_bytes=10) == b"INSIDE"
        assert tree.read("sub", "m", max_bytes=10) == b"INSIDE"


def test_nested_directory_replaced_by_symlink_is_refused(trees) -> None:  # type: ignore[no-untyped-def]
    trusted = trees["trusted"]
    with ConfinedTree.open(trusted) as tree:
        os.rename(trusted / "sub", trusted / "sub-moved")
        (trusted / "sub").symlink_to(trees["evil"] / "b" / "trusted" / "sub")
        with pytest.raises(FsError) as info:
            tree.read("sub", "m", max_bytes=10)
    assert info.value.code == "SYMLINK"


def test_file_replaced_by_symlink_is_refused(trees) -> None:  # type: ignore[no-untyped-def]
    trusted = trees["trusted"]
    with ConfinedTree.open(trusted) as tree:
        (trusted / "m").unlink()
        (trusted / "m").symlink_to(trees["evil"] / "b" / "trusted" / "m")
        assert tree.kind("m") == "symlink"
        with pytest.raises(FsError):
            tree.read("m", max_bytes=10)


def test_the_root_is_never_found_by_resolving_then_reopening_its_path(monkeypatch, trees) -> None:  # type: ignore[no-untyped-def]
    """No realpath step exists whose result could be invalidated before an open."""

    def forbidden(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("ConfinedTree must not resolve the root with realpath")

    monkeypatch.setattr(fsread.os.path, "realpath", forbidden)
    assert _read(trees["trusted"], "m") == "INSIDE"
    # Every open during acquisition is either '/' or relative to a held descriptor.
    opened: list[tuple[str, bool]] = []
    real_open = os.open

    def recording(path, flags, *args, **kwargs):  # type: ignore[no-untyped-def]
        opened.append((str(path), "dir_fd" in kwargs))
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(fsread.os, "open", recording)
    _read(trees["trusted"], "m")
    assert opened[0] == ("/", False)
    assert all(relative for _, relative in opened[1:])


def test_symlinks_in_the_roots_own_path_resolve_to_the_tree_actually_opened(trees) -> None:  # type: ignore[no-untyped-def]
    tmp = trees["tmp"]
    (tmp / "alias").symlink_to(trees["trusted"])
    (tmp / "rel").symlink_to("a/b/trusted")
    with ConfinedTree.open(tmp / "alias") as tree:
        assert tree.read("m", max_bytes=10) == b"INSIDE"
        assert tree.root == trees["trusted"].resolve()
    assert _read(tmp / "rel", "m") == "INSIDE"
    assert _read(tmp / "a" / "x" / ".." / "b" / "trusted", "m") == "refused:NOT_FOUND"
    assert _read(tmp / "a" / "b" / ".." / "b" / "trusted", "m") == "INSIDE"


def test_symlink_loops_and_non_directories_are_refused(trees) -> None:  # type: ignore[no-untyped-def]
    tmp = trees["tmp"]
    (tmp / "loop1").symlink_to(tmp / "loop2")
    (tmp / "loop2").symlink_to(tmp / "loop1")
    assert _read(tmp / "loop1", "m").startswith("refused:")
    assert _read(trees["trusted"] / "m", "x") == "refused:NOT_A_DIRECTORY"
    assert _read(tmp / "missing", "m") == "refused:NOT_FOUND"


def test_special_files_are_still_refused(trees) -> None:  # type: ignore[no-untyped-def]
    os.mkfifo(trees["trusted"] / "pipe")
    assert _read(trees["trusted"], "pipe") == "refused:NOT_A_FILE"


def test_relative_roots_are_walked_from_the_working_directory(trees, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.chdir(trees["tmp"] / "a")
    with ConfinedTree.open(Path("b/trusted")) as tree:
        assert tree.read("m", max_bytes=10) == b"INSIDE"
        assert tree.root == trees["trusted"].resolve()
