"""Local git state of a checkout, read from its metadata files. Read-only; runs no ``git`` process.

Reads the primary checkout's ``.git`` directory through ``fsread.ConfinedTree``, so no symbolic link
inside the checkout is followed: local branches (loose refs and ``packed-refs``), remote-tracking
branches, the checked-out branch, linked worktrees (``.git/worktrees/*``), and the ``origin``
remote URL from ``.git/config``.

Supported layout: a non-bare checkout whose ``.git`` is a directory using the files ref backend.
A checkout whose ``.git`` is a file (a linked worktree or submodule), or that uses the reftable
backend, is reported as unsupported and every fact about it is unknown. Remote-tracking branches
are whatever the last fetch recorded: they are never evidence about the remote itself.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from factory.fsread import ConfinedTree, FsError
from factory.refs import is_branch, is_object_id

_REF_BYTES = 4096
_PACKED_BYTES = 16 * 1024 * 1024
_CONFIG_BYTES = 1024 * 1024
_MAX_REFS = 100_000
_SECTION = re.compile(r'\[\s*([A-Za-z0-9.-]+)(?:\s+"((?:[^"\\\n]|\\.)*)")?\s*\]')
_KEY = re.compile(r"([A-Za-z][A-Za-z0-9-]*)\s*(?:=\s*(.*))?")


class GitStateError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Worktree:
    """A linked worktree. ``path`` is where git records it; it is reported, never opened."""

    id: str
    path: str | None
    branch: str | None


@dataclass(frozen=True)
class CheckoutState:
    path: str
    head_branch: str | None
    branches: dict[str, str]
    remote_branches: dict[str, dict[str, str]]
    worktrees: tuple[Worktree, ...]
    origin_url: str | None
    # Problems that did not prevent reading, e.g. a ref that is a symbolic link (it is left out).
    problems: tuple[str, ...] = field(default_factory=tuple)

    def worktrees_on(self, branch: str) -> list[Worktree]:
        return [w for w in self.worktrees if w.branch == branch]


def _text(tree: ConfinedTree, parts: tuple[str, ...], limit: int) -> str:
    try:
        return tree.read(*parts, max_bytes=limit).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise GitStateError("GIT_METADATA_INVALID", f"{'/'.join(parts)} is not UTF-8") from exc


def _symref(content: str) -> str | None:
    """The branch a ``ref: refs/heads/<branch>`` HEAD points to, or None."""
    line = content.rstrip("\n")
    if line.startswith("ref: refs/heads/"):
        name = line[len("ref: refs/heads/") :]
        return name if is_branch(name) else None
    return None


class _Reader:
    def __init__(self, tree: ConfinedTree) -> None:
        self.tree = tree
        self.problems: list[str] = []
        self.count = 0

    def loose(self, base: tuple[str, ...], prefix: str = "") -> dict[str, str]:
        """Loose refs below ``base`` (e.g. ``.git/refs/heads``), keyed by name relative to it."""
        found: dict[str, str] = {}
        try:
            entries = self.tree.list(*base)
        except FsError as exc:
            if exc.code == "NOT_FOUND":
                return found
            raise GitStateError("GIT_METADATA_UNREADABLE", exc.message) from exc
        for entry in entries:
            name = f"{prefix}{entry.name}"
            self.count += 1
            if self.count > _MAX_REFS:
                raise GitStateError("GIT_METADATA_TOO_LARGE", f"more than {_MAX_REFS} refs")
            if entry.kind == "directory":
                found.update(self.loose((*base, entry.name), f"{name}/"))
            elif entry.kind == "file":
                try:
                    value = _text(self.tree, (*base, entry.name), _REF_BYTES).strip()
                except (FsError, GitStateError) as exc:
                    self.problems.append(f"ref {name} is unreadable: {exc.message}")
                    continue
                if is_branch(name) and is_object_id(value):
                    found[name] = value
                elif not value.startswith("ref: "):
                    self.problems.append(f"ref {name} is malformed and was ignored")
            else:
                self.problems.append(f"ref {name} is a {entry.kind}, not a file, and was ignored")
        return found

    def packed(self) -> list[tuple[str, str]]:
        try:
            text = _text(self.tree, (".git", "packed-refs"), _PACKED_BYTES)
        except FsError as exc:
            if exc.code == "NOT_FOUND":
                return []
            raise GitStateError("GIT_METADATA_UNREADABLE", exc.message) from exc
        refs = []
        for line in text.splitlines():
            if not line or line.startswith(("#", "^")):
                continue
            sha, _, ref = line.partition(" ")
            if not is_object_id(sha) or not ref.startswith("refs/"):
                raise GitStateError("GIT_METADATA_INVALID", "packed-refs contains a malformed line")
            refs.append((ref, sha))
        if len(refs) > _MAX_REFS:
            raise GitStateError("GIT_METADATA_TOO_LARGE", f"more than {_MAX_REFS} packed refs")
        return refs


def _config(text: str) -> tuple[str | None, str | None]:
    """(origin URL, extensions.refStorage) from git config text. Only plain values are read."""
    section: tuple[str, str | None] | None = None
    origin: str | None = None
    storage: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        header = _SECTION.fullmatch(line)
        if header:
            section = (header.group(1).lower(), header.group(2))
            continue
        match = _KEY.fullmatch(line)
        if match is None or section is None:
            continue
        key, value = match.group(1).lower(), (match.group(2) or "").strip()
        if section == ("remote", "origin") and key == "url":
            origin = value
        elif section == ("extensions", None) and key == "refstorage":
            storage = value.lower()
    return origin, storage


def read_checkout(path: Path) -> CheckoutState:
    """Read the git state of the checkout at ``path``. Raises ``GitStateError``."""
    try:
        tree = ConfinedTree.open(path)
    except FsError as exc:
        raise GitStateError("CHECKOUT_UNREADABLE", exc.message) from exc
    with tree:
        try:
            kind = tree.kind(".git")
        except FsError as exc:
            raise GitStateError("CHECKOUT_UNREADABLE", exc.message) from exc
        if kind is None:
            raise GitStateError("NOT_A_CHECKOUT", f"{tree.root} has no .git directory")
        if kind == "file":
            raise GitStateError(
                "CHECKOUT_LAYOUT_UNSUPPORTED",
                f"{tree.root}/.git is a file (linked worktree or submodule); use the repository's primary checkout",
            )
        if kind != "directory":
            raise GitStateError("CHECKOUT_UNSAFE", f"{tree.root}/.git is a {kind}; it is not followed")
        try:
            origin, storage = _config(_text(tree, (".git", "config"), _CONFIG_BYTES))
            reftable = tree.kind(".git", "reftable") is not None
        except FsError as exc:
            raise GitStateError("GIT_METADATA_UNREADABLE", exc.message) from exc
        if reftable or (storage is not None and storage != "files"):
            raise GitStateError("CHECKOUT_LAYOUT_UNSUPPORTED", "the reftable ref backend is not supported")

        reader = _Reader(tree)
        branches: dict[str, str] = {}
        remotes: dict[str, dict[str, str]] = {}
        for ref, sha in reader.packed():
            if ref.startswith("refs/heads/") and is_branch(ref[len("refs/heads/") :]):
                branches[ref[len("refs/heads/") :]] = sha
            elif ref.startswith("refs/remotes/"):
                remote, _, name = ref[len("refs/remotes/") :].partition("/")
                if name and name != "HEAD" and is_branch(name):
                    remotes.setdefault(remote, {})[name] = sha
        branches.update(reader.loose((".git", "refs", "heads")))
        for remote, found in reader.loose((".git", "refs", "remotes")).items():
            name, _, branch = remote.partition("/")
            if branch and branch != "HEAD":
                remotes.setdefault(name, {})[branch] = found

        try:
            head = _symref(_text(tree, (".git", "HEAD"), _REF_BYTES))
        except FsError as exc:
            raise GitStateError("GIT_METADATA_UNREADABLE", exc.message) from exc

        worktrees: list[Worktree] = []
        try:
            entries = tree.list(".git", "worktrees")
        except FsError as exc:
            if exc.code != "NOT_FOUND":
                raise GitStateError("GIT_METADATA_UNREADABLE", exc.message) from exc
            entries = []
        for entry in entries:
            if entry.kind != "directory":
                reader.problems.append(f"worktree entry {entry.name} is a {entry.kind} and was ignored")
                continue
            try:
                branch = _symref(_text(tree, (".git", "worktrees", entry.name, "HEAD"), _REF_BYTES))
            except (FsError, GitStateError) as exc:
                reader.problems.append(f"worktree {entry.name} HEAD is unreadable: {getattr(exc, 'message', exc)}")
                branch = None
            try:
                gitdir = _text(tree, (".git", "worktrees", entry.name, "gitdir"), _REF_BYTES).strip()
                location: str | None = gitdir.removesuffix("/.git") if gitdir else None
            except (FsError, GitStateError):
                location = None
            worktrees.append(Worktree(entry.name, location, branch))

        return CheckoutState(
            path=str(tree.root),
            head_branch=head,
            branches=dict(sorted(branches.items())),
            remote_branches={k: dict(sorted(v.items())) for k, v in sorted(remotes.items())},
            worktrees=tuple(worktrees),
            origin_url=origin,
            problems=tuple(sorted(reader.problems)),
        )


def github_slug(url: str | None) -> str | None:
    """``owner/name`` of a GitHub remote URL (https, ssh or scp-like), lower-cased; None otherwise."""
    if not url:
        return None
    match = re.fullmatch(
        r"(?:https://github\.com/|ssh://git@github\.com/|git@github\.com:)"
        r"([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/([A-Za-z0-9._-]{1,100}?)(?:\.git)?/?",
        url,
    )
    return f"{match.group(1)}/{match.group(2)}".lower() if match else None
