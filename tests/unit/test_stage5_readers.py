"""Stage 5 readers: strict value validation, confined file access and local git state."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from stage5_support import HEAD_SHA, ORIGIN, make_checkout
from support import fake_sha

from factory import refs
from factory.fsread import ConfinedTree, FsError
from factory.gitstate import GitStateError, github_slug, read_checkout

# -- refs ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["next", "work/APP-1-r3", "release/2.x", "a.b_c-d"])
def test_command_branches_accept_plain_names(value: str) -> None:
    assert refs.is_command_branch(value)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "-next",
        "work/-r1",
        "--base=stable",
        "next\n",
        "next\nstable",
        "a..b",
        "a//b",
        "/a",
        "a/",
        "a.lock",
        "a/.hidden",
        "a b",
        "a\tb",
        "a~1",
        "a^",
        "a:b",
        "a?",
        "a*",
        "a[",
        "a\\b",
        "a@{1}",
        "@",
        "HEAD",
        "x\x00y",
        "x\x7f",
        "$(id)",
        "`id`",
        "a;b",
        "x" * 201,
    ],
)
def test_command_branches_refuse_unsafe_names(value: str) -> None:
    assert not refs.is_command_branch(value)


@pytest.mark.parametrize("value", ["APP-1\n", "APP 1", "../APP", "APP/1", "-APP", "APP-", "", "a" * 81])
def test_task_ids_are_matched_in_full(value: str) -> None:
    assert not refs.is_task_id(value)


def test_sha_and_version_checks_use_full_matches() -> None:
    sha = fake_sha("x")
    assert refs.is_sha(sha) and not refs.is_sha(sha + "\n") and not refs.is_sha(sha.upper())
    assert refs.is_object_id("a" * 64) and not refs.is_object_id("a" * 63)
    assert refs.is_version("0.14.0") and refs.is_version("1.0.0-rc.1")
    for bad in ("0.14", "v0.14.0", "0.14.0\n", "00.1.0", "0.14.0+build"):
        assert not refs.is_version(bad)


@pytest.mark.parametrize("value", ["\x1b[2J", "a\rb", "a\x00", "\u202eevil", "a\u200bb", "\x85"])
def test_unsafe_text_finds_control_and_formatting_characters(value: str) -> None:
    assert refs.unsafe_text(value) is not None


def test_unsafe_text_allows_tabs_newlines_and_unicode_letters() -> None:
    assert refs.unsafe_text("Ürün listesi\n\t- çıktı") is None


# -- confined reads --------------------------------------------------------------------------------


def test_confined_tree_reads_regular_files(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "f.txt").write_text("hello")
    with ConfinedTree.open(tmp_path) as tree:
        assert tree.read("a", "f.txt", max_bytes=10) == b"hello"
        assert tree.kind("a") == "directory" and tree.kind("a", "f.txt") == "file" and tree.kind("nope") is None
        assert [e.name for e in tree.list("a")] == ["f.txt"]


@pytest.mark.parametrize("part", ["..", ".", "", "a/b", "x\x00"])
def test_confined_tree_refuses_unsafe_components(tmp_path: Path, part: str) -> None:
    with ConfinedTree.open(tmp_path) as tree, pytest.raises(FsError) as info:
        tree.read(part, max_bytes=10)
    assert info.value.code == "UNSAFE_PATH"


def test_confined_tree_never_follows_symbolic_links(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("secret")
    root = tmp_path / "root"
    root.mkdir()
    (root / "file-link").symlink_to(outside / "secret")
    (root / "dir-link").symlink_to(outside)
    with ConfinedTree.open(root) as tree:
        for parts in (("file-link",), ("dir-link", "secret")):
            with pytest.raises(FsError) as info:
                tree.read(*parts, max_bytes=100)
            assert info.value.code == "SYMLINK"
        assert tree.kind("file-link") == "symlink"
        assert {e.name: e.kind for e in tree.list()} == {"dir-link": "symlink", "file-link": "symlink"}


def test_confined_tree_refuses_hard_links_when_asked(tmp_path: Path) -> None:
    (tmp_path / "f").write_text("x")
    os.link(tmp_path / "f", tmp_path / "g")
    with ConfinedTree.open(tmp_path) as tree:
        assert tree.read("f", max_bytes=10) == b"x"
        with pytest.raises(FsError) as info:
            tree.read("f", max_bytes=10, single_link=True)
    assert info.value.code == "HARD_LINKED"


def test_confined_tree_refuses_fifos_without_blocking(tmp_path: Path) -> None:
    os.mkfifo(tmp_path / "pipe")
    with ConfinedTree.open(tmp_path) as tree, pytest.raises(FsError) as info:
        tree.read("pipe", max_bytes=10)
    assert info.value.code == "NOT_A_FILE"


def test_confined_tree_bounds_file_size(tmp_path: Path) -> None:
    (tmp_path / "big").write_bytes(b"x" * 11)
    with ConfinedTree.open(tmp_path) as tree, pytest.raises(FsError) as info:
        tree.read("big", max_bytes=10)
    assert info.value.code == "TOO_LARGE"


def test_confined_tree_root_must_exist(tmp_path: Path) -> None:
    with pytest.raises(FsError) as info:
        ConfinedTree.open(tmp_path / "missing")
    assert info.value.code == "NOT_FOUND"


# -- git state -------------------------------------------------------------------------------------


def test_checkout_state_from_loose_packed_remote_and_worktree_refs(tmp_path: Path) -> None:
    make_checkout(
        tmp_path,
        branches={"work/APP-1-r3": fake_sha("r3")},
        packed={"work/APP-1-r1": fake_sha("r1"), "next": fake_sha("stale packed next")},
        remotes={"work/APP-1-r2": fake_sha("r2")},
        worktrees={"wt1": "work/APP-1-r3"},
    )
    state = read_checkout(tmp_path)
    assert state.head_branch == "next"
    # Loose refs override packed ones.
    assert state.branches == {"next": HEAD_SHA, "work/APP-1-r1": fake_sha("r1"), "work/APP-1-r3": fake_sha("r3")}
    assert state.remote_branches == {"origin": {"work/APP-1-r2": fake_sha("r2")}}
    assert [w.branch for w in state.worktrees_on("work/APP-1-r3")] == ["work/APP-1-r3"]
    assert state.origin_url == ORIGIN


def test_symbolic_link_refs_are_ignored_and_reported(tmp_path: Path) -> None:
    make_checkout(tmp_path)
    target = tmp_path / "elsewhere"
    target.write_text(fake_sha("x") + "\n")
    (tmp_path / ".git" / "refs" / "heads" / "evil").symlink_to(target)
    state = read_checkout(tmp_path)
    assert "evil" not in state.branches
    assert any("evil" in problem and "symlink" in problem for problem in state.problems)


def test_git_directory_as_symbolic_link_is_refused(tmp_path: Path) -> None:
    real = make_checkout(tmp_path / "real")
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / ".git").symlink_to(real / ".git")
    with pytest.raises(GitStateError) as info:
        read_checkout(checkout)
    assert info.value.code == "CHECKOUT_UNSAFE"


@pytest.mark.parametrize(
    ("setup", "code"),
    [
        (lambda p: p.mkdir(), "NOT_A_CHECKOUT"),
        (
            lambda p: (p.mkdir(), (p / ".git").write_text("gitdir: /x/.git/worktrees/a\n")),
            "CHECKOUT_LAYOUT_UNSUPPORTED",
        ),
        (lambda p: (make_checkout(p), (p / ".git" / "reftable").mkdir()), "CHECKOUT_LAYOUT_UNSUPPORTED"),
        (
            lambda p: (make_checkout(p), (p / ".git" / "packed-refs").write_text("garbage line\n")),
            "GIT_METADATA_INVALID",
        ),
    ],
)
def test_unsupported_or_malformed_checkouts(tmp_path: Path, setup, code: str) -> None:  # type: ignore[no-untyped-def]
    checkout = tmp_path / "c"
    setup(checkout)
    with pytest.raises(GitStateError) as info:
        read_checkout(checkout)
    assert info.value.code == code


@pytest.mark.parametrize(
    ("url", "slug"),
    [
        ("https://github.com/Example-Org/example-app.git", "example-org/example-app"),
        ("git@github.com:example-org/example-app.git", "example-org/example-app"),
        ("ssh://git@github.com/example-org/example-app", "example-org/example-app"),
        ("https://github.com.evil.example/example-org/example-app.git", None),
        ("https://gitlab.com/example-org/example-app.git", None),
        ("https://github.com/example-org/example-app.git\n", None),
        (None, None),
    ],
)
def test_github_slug(url: str | None, slug: str | None) -> None:
    assert github_slug(url) == slug


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_reader_agrees_with_real_git(tmp_path: Path) -> None:
    """Build a repository with git itself (local only), then compare with what the reader sees."""
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}

    def git(*args: str, cwd: Path = tmp_path / "repo") -> str:
        result = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=True)
        return result.stdout.strip()

    repo = tmp_path / "repo"
    repo.mkdir()
    git("init", "--quiet", "--initial-branch=next")
    git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "--quiet", "--allow-empty", "-m", "c")
    git("remote", "add", "origin", ORIGIN)
    git("branch", "work/APP-1-r1")
    git("branch", "work/APP-1-r2")
    git("pack-refs", "--all")
    git("branch", "work/APP-1-r3")
    git("worktree", "add", "--quiet", str(tmp_path / "wt"), "work/APP-1-r3")
    expected = dict(
        line.split(" ", 1)[::-1]
        for line in git("for-each-ref", "--format=%(objectname) %(refname:short)", "refs/heads").splitlines()
    )

    state = read_checkout(repo)
    assert state.branches == expected
    assert state.head_branch == "next"
    assert [(w.branch, w.path) for w in state.worktrees] == [("work/APP-1-r3", str((tmp_path / "wt").resolve()))]
    assert github_slug(state.origin_url) == "example-org/example-app"
