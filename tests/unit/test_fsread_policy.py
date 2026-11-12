"""S3: the permission policy of fsread, on real filesystem objects.

Trust is never inferred from group membership, and ACLs are evaluated, not ignored. macOS ACL tests
use real ACLs (``chmod +a``); Linux ACL tests use ``setfacl`` when it is installed. Neither platform's
ACL behavior is claimed verified by tests that only ran on the other.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from factory import fsread
from factory.fsread import ConfinedTree, FsError

darwin = pytest.mark.skipif(sys.platform != "darwin", reason="macOS ACLs")
linux = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux ACLs")


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    (root / "m").write_text("INSIDE")
    (root / "sub" / "m").write_text("INSIDE")
    return root


def _read(path: Path, *parts: str) -> str:
    try:
        with ConfinedTree.open(path) as confined:
            return confined.read(*parts, max_bytes=100).decode()
    except FsError as exc:
        return f"refused:{exc.code}"


def _chmod_acl(path: Path, rule: str) -> None:
    subprocess.run(["chmod", "+a", rule, str(path)], check=True, capture_output=True)


def test_user_owned_private_tree_is_accepted(tree: Path) -> None:
    assert _read(tree, "m") == "INSIDE"
    assert _read(tree, "sub", "m") == "INSIDE"


@pytest.mark.parametrize("mode", [0o775, 0o770, 0o757, 0o777])
def test_group_or_world_writable_root_is_refused(tree: Path, mode: int) -> None:
    os.chmod(tree, mode)
    try:
        assert _read(tree, "m") == "refused:ROOT_PATH_UNSAFE"
    finally:
        os.chmod(tree, 0o755)


def test_group_writable_with_the_users_own_primary_group_is_still_refused(tree: Path) -> None:
    """Exclusivity of a group cannot be established authoritatively, so a private-looking group is
    not trusted either."""
    os.chown(tree, -1, os.getgid())
    os.chmod(tree, 0o775)
    try:
        assert _read(tree, "m") == "refused:ROOT_PATH_UNSAFE"
    finally:
        os.chmod(tree, 0o755)


@darwin
def test_macos_everyone_group_writable_is_refused(tree: Path) -> None:
    try:
        os.chown(tree, -1, 12)  # 'everyone': every user is an implicit member; gr_mem is empty
    except PermissionError:
        pytest.skip("cannot change the group to everyone here")
    os.chmod(tree, 0o775)
    try:
        assert _read(tree, "m") == "refused:ROOT_PATH_UNSAFE"
    finally:
        os.chmod(tree, 0o755)


def test_group_writable_directory_and_file_below_the_root_are_refused(tree: Path) -> None:
    os.chmod(tree / "sub", 0o775)
    try:
        assert _read(tree, "sub", "m") == "refused:UNSAFE_PERMISSIONS"
    finally:
        os.chmod(tree / "sub", 0o755)
    os.chmod(tree / "m", 0o664)
    try:
        assert _read(tree, "m") == "refused:UNSAFE_PERMISSIONS"
    finally:
        os.chmod(tree / "m", 0o644)


def test_sticky_shared_directory_is_accepted_and_world_writable_file_is_not(tree: Path) -> None:
    os.chmod(tree, 0o1777)
    try:
        assert _read(tree, "m") == "INSIDE"
        os.chmod(tree / "m", 0o666)
        assert _read(tree, "m") == "refused:UNSAFE_PERMISSIONS"
    finally:
        os.chmod(tree, 0o755)
        os.chmod(tree / "m", 0o644)


def test_root_owned_directories_are_accepted() -> None:
    with ConfinedTree.open(Path("/")) as confined:
        assert confined.kind("this-entry-does-not-exist-factory") is None


def test_permission_change_after_listing_is_refused(tree: Path) -> None:
    with ConfinedTree.open(tree) as confined:
        assert [e.name for e in confined.list("sub")] == ["m"]
        os.chmod(tree / "sub", 0o777)
        try:
            with pytest.raises(FsError) as info:
                confined.read("sub", "m", max_bytes=10)
        finally:
            os.chmod(tree / "sub", 0o755)
    assert info.value.code == "UNSAFE_PERMISSIONS"


def test_unevaluable_platform_fails_closed(tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fsread.sys, "platform", "sunos5")
    assert _read(tree, "m") == "refused:PERMISSIONS_UNVERIFIABLE"


@darwin
@pytest.mark.parametrize(
    "rule",
    [
        "everyone allow add_file",
        "everyone allow add_subdirectory",
        "everyone allow delete_child",
        "everyone allow delete",
        "everyone allow writeattr",
        "everyone allow writeextattr",
        "everyone allow writesecurity",
        "everyone allow chown",
        "group:staff allow add_file,delete_child",
        f"user:{os.environ.get('USER', 'root')} allow add_file",
    ],
)
def test_macos_acl_granting_modification_is_refused_despite_safe_mode_bits(tree: Path, rule: str) -> None:
    _chmod_acl(tree, rule)
    assert oct(tree.stat().st_mode & 0o777) == "0o755"  # mode bits look safe
    assert _read(tree, "m") == "refused:ROOT_PATH_UNSAFE"


@darwin
def test_macos_acl_on_a_file_below_the_root_is_refused(tree: Path) -> None:
    _chmod_acl(tree / "m", "everyone allow write")
    assert _read(tree, "m") == "refused:UNSAFE_PERMISSIONS"


@darwin
@pytest.mark.parametrize(
    "rule", ["everyone deny delete", "group:everyone deny delete", "everyone allow list,search,readattr"]
)
def test_macos_deny_and_read_only_acls_are_accepted(tree: Path, rule: str) -> None:
    _chmod_acl(tree, rule)
    assert _read(tree, "m") == "INSIDE"


@darwin
def test_macos_acl_added_after_listing_is_refused(tree: Path) -> None:
    with ConfinedTree.open(tree) as confined:
        confined.list("sub")
        _chmod_acl(tree / "sub", "everyone allow add_file")
        with pytest.raises(FsError) as info:
            confined.read("sub", "m", max_bytes=10)
    assert info.value.code == "UNSAFE_PERMISSIONS"


@linux
@pytest.mark.skipif(shutil.which("setfacl") is None, reason="setfacl is not installed")
def test_linux_posix_acl_granting_write_shows_in_the_group_class_and_is_refused(tree: Path) -> None:
    result = subprocess.run(["setfacl", "-m", "u:nobody:rwx", str(tree)], capture_output=True)
    if result.returncode != 0:
        pytest.skip(f"filesystem does not support POSIX ACLs: {result.stderr.decode().strip()}")
    assert tree.stat().st_mode & 0o020  # the ACL mask is the group-class bits
    assert _read(tree, "m") == "refused:ROOT_PATH_UNSAFE"


@linux
def test_linux_unevaluated_acl_kinds_fail_closed(monkeypatch: pytest.MonkeyPatch, tree: Path) -> None:
    monkeypatch.setattr(fsread.os, "listxattr", lambda _fd: ["system.nfs4_acl"])
    assert _read(tree, "m") == "refused:PERMISSIONS_UNVERIFIABLE"


def test_objects_created_under_umask_002_are_refused(tmp_path: Path) -> None:
    """The default on user-private-group hosts and GitHub runners: refused, by design."""
    previous = os.umask(0o002)
    try:
        root = tmp_path / "umask002"
        root.mkdir()
        (root / "m").write_text("INSIDE")
    finally:
        os.umask(previous)
    assert oct(root.stat().st_mode & 0o777) == "0o775"
    assert _read(root, "m") == "refused:ROOT_PATH_UNSAFE"
