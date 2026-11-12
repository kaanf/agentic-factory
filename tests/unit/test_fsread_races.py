"""F3/R2 regression: a confined root never silently switches to another tree.

Each test builds a trusted tree (marker ``INSIDE``) and an attacker tree (marker ``OUTSIDE``) with
real filesystem objects, and changes the filesystem at a precise point of the acquisition by
intercepting calls inside ``factory.fsread``. Accepted outcomes are the trusted marker or an explicit
refusal; each test asserts the exact one. ``OUTSIDE`` is never acceptable.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Callable
from pathlib import Path

import pytest

from factory import fsread
from factory.fsread import ConfinedTree, FsError


@pytest.fixture
def trees(tmp_path: Path) -> dict[str, Path]:
    trusted = tmp_path / "a" / "b" / "trusted"
    (trusted / "sub").mkdir(parents=True)
    (trusted / "m").write_text("INSIDE")
    (trusted / "sub" / "m").write_text("INSIDE")
    evil = tmp_path / "evil"
    (evil / "b" / "trusted" / "sub").mkdir(parents=True)
    (evil / "b" / "trusted" / "m").write_text("OUTSIDE")
    (evil / "b" / "trusted" / "sub" / "m").write_text("OUTSIDE")
    return {"tmp": tmp_path, "trusted": trusted, "evil": evil}


def _after_open(monkeypatch: pytest.MonkeyPatch, name: str, action: Callable[[], None]) -> list[str]:
    """Run ``action`` once, right after fsread opens a component called ``name``."""
    real_open = os.open
    seen: list[str] = []

    def opening(path, flags, *args, **kwargs):  # type: ignore[no-untyped-def]
        fd = real_open(path, flags, *args, **kwargs)
        seen.append(str(path))
        if str(path) == name and seen.count(name) == 1:
            action()
        return fd

    monkeypatch.setattr(fsread.os, "open", opening)
    return seen


def _before_open(monkeypatch: pytest.MonkeyPatch, name: str, action: Callable[[], None]) -> None:
    """Run ``action`` once, after fsread has examined ``name`` and just before it opens it."""
    real_open = os.open
    done: list[bool] = []

    def opening(path, flags, *args, **kwargs):  # type: ignore[no-untyped-def]
        if str(path) == name and not done:
            done.append(True)
            action()
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(fsread.os, "open", opening)


def _read(path: Path, *parts: str) -> str:
    try:
        with ConfinedTree.open(path) as tree:
            return tree.read(*parts, max_bytes=100).decode()
    except FsError as exc:
        return f"refused:{exc.code}"


# 1, 7: ancestor and parent replaced during acquisition


def test_ancestor_swapped_after_it_was_opened_is_refused(trees, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    tmp = trees["tmp"]

    def swap() -> None:
        os.rename(tmp / "a", tmp / "a-moved")
        (tmp / "a").symlink_to(trees["evil"])

    _after_open(monkeypatch, "a", swap)
    assert _read(trees["trusted"], "m") == "refused:ROOT_CHANGED_DURING_ACQUISITION"
    assert (tmp / "a").is_symlink()  # the swap really happened mid-acquisition


def test_parent_replaced_by_a_real_directory_is_refused(trees, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    tmp = trees["tmp"]

    def swap() -> None:
        os.rename(tmp / "a" / "b", tmp / "b-moved")
        os.rename(trees["evil"] / "b", tmp / "a" / "b")

    _after_open(monkeypatch, "b", swap)
    assert _read(trees["trusted"], "m") == "refused:ROOT_CHANGED_DURING_ACQUISITION"
    assert (tmp / "b-moved").is_dir()


def test_root_replaced_while_it_is_opened_is_refused(trees, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    trusted = trees["trusted"]

    def swap() -> None:
        os.rename(trusted, trusted.parent / "trusted-moved")
        os.rename(trees["evil"] / "b" / "trusted", trusted)

    _after_open(monkeypatch, "trusted", swap)
    assert _read(trusted, "m") == "refused:ROOT_CHANGED_DURING_ACQUISITION"


# 6, 8: concurrent rename and identity mismatch between examination and open


def test_entry_swapped_between_examination_and_open_is_refused(trees, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    tmp = trees["tmp"]

    def swap() -> None:
        os.rename(tmp / "a" / "b", tmp / "b-moved")
        os.rename(trees["evil"] / "b", tmp / "a" / "b")

    _before_open(monkeypatch, "b", swap)
    assert _read(trees["trusted"], "m") == "refused:ROOT_CHANGED_DURING_ACQUISITION"


def test_concurrent_unrelated_rename_in_a_parent_is_refused(trees, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    tmp = trees["tmp"]
    (tmp / "a" / "other").mkdir()

    def rename() -> None:
        os.rename(tmp / "a" / "other", tmp / "a" / "other2")

    _before_open(monkeypatch, "b", rename)
    assert _read(trees["trusted"], "m") == "refused:ROOT_CHANGED_DURING_ACQUISITION"


def test_root_identity_change_after_acquisition_does_not_change_the_tree(trees) -> None:  # type: ignore[no-untyped-def]
    trusted = trees["trusted"]
    with ConfinedTree.open(trusted) as tree:
        os.rename(trusted, trusted.parent / "gone")
        os.rename(trees["evil"] / "b" / "trusted", trusted)
        assert tree.read("m", max_bytes=10) == b"INSIDE"
        assert tree.read("sub", "m", max_bytes=10) == b"INSIDE"


def test_nested_directory_replaced_by_symlink_below_the_root_is_refused(trees) -> None:  # type: ignore[no-untyped-def]
    trusted = trees["trusted"]
    with ConfinedTree.open(trusted) as tree:
        os.rename(trusted / "sub", trusted / "sub-moved")
        (trusted / "sub").symlink_to(trees["evil"] / "b" / "trusted" / "sub")
        with pytest.raises(FsError) as info:
            tree.read("sub", "m", max_bytes=10)
    assert info.value.code == "SYMLINK"


# 2, 3, 4, 5: symbolic links in the configured root path


def test_absolute_root_symlink_is_refused_even_before_any_swap(trees) -> None:  # type: ignore[no-untyped-def]
    link = trees["tmp"] / "configured"
    link.symlink_to(trees["trusted"])
    assert _read(link, "m") == "refused:ROOT_PATH_SYMLINK_UNTRUSTED"


def test_absolute_root_symlink_with_ancestor_swap_is_refused(trees, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    tmp = trees["tmp"]
    link = tmp / "configured"
    link.symlink_to(trees["trusted"])

    def swap() -> None:
        os.rename(tmp / "a", tmp / "a-moved")
        os.rename(trees["evil"], tmp / "a")

    _after_open(monkeypatch, tmp.name, swap)
    assert _read(link, "m") == "refused:ROOT_PATH_SYMLINK_UNTRUSTED"


def test_relative_root_symlink_with_ancestor_swap_is_refused(trees, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    tmp = trees["tmp"]
    (tmp / "rel").symlink_to("a/b/trusted")

    def swap() -> None:
        os.rename(tmp / "a", tmp / "a-moved")
        os.rename(trees["evil"], tmp / "a")

    _after_open(monkeypatch, tmp.name, swap)
    assert _read(tmp / "rel", "m") == "refused:ROOT_PATH_SYMLINK_UNTRUSTED"


def test_nested_symlink_chains_and_symlinked_ancestors_are_refused(trees) -> None:  # type: ignore[no-untyped-def]
    tmp = trees["tmp"]
    (tmp / "hop2").symlink_to(trees["trusted"])
    (tmp / "hop1").symlink_to(tmp / "hop2")
    (tmp / "alias-a").symlink_to(tmp / "a")
    assert _read(tmp / "hop1", "m") == "refused:ROOT_PATH_SYMLINK_UNTRUSTED"
    assert _read(tmp / "alias-a" / "b" / "trusted", "m") == "refused:ROOT_PATH_SYMLINK_UNTRUSTED"


def test_symlink_cycles_are_refused(trees) -> None:  # type: ignore[no-untyped-def]
    tmp = trees["tmp"]
    (tmp / "loop1").symlink_to(tmp / "loop2")
    (tmp / "loop2").symlink_to(tmp / "loop1")
    assert _read(tmp / "loop1", "m") == "refused:ROOT_PATH_SYMLINK_UNTRUSTED"


def _system_link() -> str | None:
    for candidate in ("/tmp", "/var", "/etc", "/bin", "/lib", "/sbin"):
        info = os.lstat(candidate) if os.path.lexists(candidate) else None
        if info is not None and stat.S_ISLNK(info.st_mode) and info.st_uid == 0:
            if not os.readlink(candidate).startswith("/"):
                return candidate
    return None


@pytest.mark.skipif(_system_link() is None, reason="no root-controlled relative system link on this host")
def test_root_controlled_relative_system_links_are_followed() -> None:
    link = _system_link()
    assert link is not None
    with ConfinedTree.open(Path(link)) as tree:
        assert str(tree.root) == os.path.realpath(link)


# ancestors modifiable by other principals


def test_world_writable_ancestor_is_refused(trees) -> None:  # type: ignore[no-untyped-def]
    shared = trees["tmp"] / "a"
    os.chmod(shared, 0o777)
    try:
        assert _read(trees["trusted"], "m") == "refused:ROOT_PATH_UNSAFE"
    finally:
        os.chmod(shared, 0o755)


def test_group_writable_root_is_refused(trees) -> None:  # type: ignore[no-untyped-def]
    os.chmod(trees["trusted"], 0o775)
    try:
        assert _read(trees["trusted"], "m") == "refused:ROOT_PATH_UNSAFE"
    finally:
        os.chmod(trees["trusted"], 0o755)


def test_sticky_shared_directory_with_own_entry_is_accepted(trees) -> None:  # type: ignore[no-untyped-def]
    shared = trees["tmp"] / "a"
    os.chmod(shared, 0o1777)
    try:
        assert _read(trees["trusted"], "m") == "INSIDE"
    finally:
        os.chmod(shared, 0o755)


# 9, 10: special files and traversal


def test_special_files_in_and_below_the_root_path(trees) -> None:  # type: ignore[no-untyped-def]
    os.mkfifo(trees["trusted"] / "pipe")
    assert _read(trees["trusted"], "pipe") == "refused:NOT_A_FILE"
    assert _read(trees["trusted"] / "pipe", "x") == "refused:NOT_A_DIRECTORY"
    assert _read(trees["trusted"] / "m", "x") == "refused:NOT_A_DIRECTORY"
    assert _read(trees["tmp"] / "missing", "m") == "refused:NOT_FOUND"


@pytest.mark.parametrize("part", ["..", ".", "", "a/b", "x\x00"])
def test_traversal_below_the_root_is_refused(trees, part: str) -> None:  # type: ignore[no-untyped-def]
    assert _read(trees["trusted"], part) == "refused:UNSAFE_PATH"


def test_dot_dot_in_the_configured_path_is_physical(trees) -> None:  # type: ignore[no-untyped-def]
    tmp = trees["tmp"]
    assert _read(tmp / "a" / "b" / ".." / "b" / "trusted", "m") == "INSIDE"
    assert _read(tmp / "a" / "x" / ".." / "b" / "trusted", "m") == "refused:NOT_FOUND"


def test_relative_roots_are_anchored_at_the_working_directory(trees, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.chdir(trees["tmp"] / "a")
    with ConfinedTree.open(Path("b/trusted")) as tree:
        assert tree.read("m", max_bytes=10) == b"INSIDE"
        assert tree.root == trees["trusted"].resolve()


def test_the_root_is_never_found_by_resolving_then_reopening_its_path(monkeypatch, trees) -> None:  # type: ignore[no-untyped-def]
    def forbidden(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("ConfinedTree must not resolve the root with realpath")

    monkeypatch.setattr(fsread.os.path, "realpath", forbidden)
    opened: list[tuple[str, bool]] = []
    real_open = os.open

    def recording(path, flags, *args, **kwargs):  # type: ignore[no-untyped-def]
        opened.append((str(path), "dir_fd" in kwargs))
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(fsread.os, "open", recording)
    assert _read(trees["trusted"], "m") == "INSIDE"
    assert opened[0] == ("/", False)
    assert all(relative for _, relative in opened[1:])
    assert sum(1 for path, _ in opened if path == "/") == 1  # no restart from /
