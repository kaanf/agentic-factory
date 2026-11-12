"""Local git state of a checkout, read from its metadata files. Read-only; runs no ``git`` process.

Reads the primary checkout's ``.git`` directory through ``fsread.ConfinedTree``, so no symbolic link
inside the checkout is followed: local branches (loose refs and ``packed-refs``), remote-tracking
branches, the checked-out branch, linked worktrees (``.git/worktrees/*``), and the ``origin``
remote URL from ``.git/config``.

Absence is a confirmed fact, never a default. Every ref entry that exists but cannot be resolved (a
symbolic ref, a symbolic link, a malformed or unreadable value) is kept as *present but unresolved*;
a ref directory that cannot be listed makes every name below it *unknown*; a linked worktree whose
``HEAD`` cannot be read makes "is this branch checked out?" unknown. Symbolic refs are never
followed. ``CheckoutState.branch_state`` and its siblings answer ``present``, ``absent`` or
``unknown``; only ``absent`` means the name was looked for and is not there.

Supported layout: a non-bare checkout whose ``.git`` is a directory using the files ref backend.
A checkout whose ``.git`` is a file (a linked worktree or submodule), or that uses the reftable
backend, is reported as unsupported and every fact about it is unknown. Remote-tracking branches
are whatever the last fetch recorded: they are never evidence about the remote itself.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from factory.fsread import INTEGRITY_CODES, ConfinedTree, FsError
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


PRESENT = "present"
ABSENT = "absent"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class Worktree:
    """A linked worktree. ``path`` is where git records it; it is reported, never opened.

    ``state`` is ``branch`` (``branch`` is set), ``detached`` or ``unknown`` (its HEAD could not be
    read or understood, so it may have any branch checked out).
    """

    id: str
    path: str | None
    branch: str | None
    state: str = "branch"


def _covers(prefix: str, name: str) -> bool:
    return prefix == "" or name == prefix or name.startswith(prefix + "/")


@dataclass
class RefSet:
    """Refs of one namespace (``refs/heads`` or ``refs/remotes``), named relative to it.

    ``unresolved`` maps names that exist but whose target could not be established to the reason.
    ``unknown`` lists subtrees that could not be listed ("" for the whole namespace).
    """

    resolved: dict[str, str] = field(default_factory=dict)
    unresolved: dict[str, str] = field(default_factory=dict)
    unknown: tuple[str, ...] = ()

    def state(self, name: str) -> str:
        if name in self.resolved or name in self.unresolved:
            return PRESENT
        if any(_covers(prefix, name) for prefix in self.unknown):
            return UNKNOWN
        return ABSENT

    def names(self) -> list[str]:
        return sorted({*self.resolved, *self.unresolved})


@dataclass(frozen=True)
class CheckoutState:
    path: str
    head_branch: str | None
    local: RefSet
    remote: RefSet
    worktrees: tuple[Worktree, ...]
    origin_url: str | None
    # False when HEAD exists but is neither a branch reference nor an object id.
    head_known: bool = True
    # Human-readable notes about everything that could not be resolved.
    problems: tuple[str, ...] = field(default_factory=tuple)

    @property
    def branches(self) -> dict[str, str]:
        """Local branches whose target is known (resolved refs only)."""
        return self.local.resolved

    @property
    def remote_branches(self) -> dict[str, dict[str, str]]:
        found: dict[str, dict[str, str]] = {}
        for name, sha in self.remote.resolved.items():
            remote, _, branch = name.partition("/")
            found.setdefault(remote, {})[branch] = sha
        return found

    def branch_state(self, branch: str) -> str:
        return self.local.state(branch)

    def remote_tracking_state(self, branch: str) -> str:
        """Whether any remote-tracking ref ``<remote>/<branch>`` exists in the last fetch."""
        if any(name.partition("/")[2] == branch for name in self.remote.names()):
            return PRESENT
        for prefix in self.remote.unknown:
            remote = prefix.split("/")[0]
            if prefix == "" or _covers(prefix, f"{remote}/{branch}"):
                return UNKNOWN
        return ABSENT

    def checked_out_state(self, branch: str) -> str:
        """Whether ``branch`` is checked out anywhere: the primary checkout's HEAD or a linked worktree."""
        if self.head_branch == branch or any(w.branch == branch for w in self.worktrees):
            return PRESENT
        if not self.head_known or any(w.state == UNKNOWN for w in self.worktrees):
            return UNKNOWN
        return ABSENT

    def head_target_unresolved(self, branch: str) -> bool:
        """The primary HEAD names ``branch`` but no ref for it resolves (an unborn or dangling target)."""
        return self.head_branch == branch and branch not in self.local.resolved

    def worktree_state(self, branch: str) -> str:
        if any(w.branch == branch for w in self.worktrees):
            return PRESENT
        if any(w.state == UNKNOWN for w in self.worktrees):
            return UNKNOWN
        return ABSENT

    def worktrees_on(self, branch: str) -> list[Worktree]:
        return [w for w in self.worktrees if w.branch == branch]


def _metadata_error(exc: FsError) -> GitStateError:
    """An unreadable piece of git metadata; integrity failures keep their own code."""
    return GitStateError(exc.code if exc.code in INTEGRITY_CODES else "GIT_METADATA_UNREADABLE", exc.message)


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


def _head(content: str) -> tuple[str, str | None]:
    """Classify a HEAD file: (``branch``, name), (``detached``, None) or (``unknown``, None)."""
    branch = _symref(content)
    if branch is not None:
        return "branch", branch
    if is_object_id(content.strip()) and content.strip() + "\n" in (content, content + "\n"):
        return "detached", None
    return UNKNOWN, None


class _Reader:
    def __init__(self, tree: ConfinedTree) -> None:
        self.tree = tree
        self.problems: list[str] = []
        self.count = 0

    def loose(self, base: tuple[str, ...], refs: RefSet, prefix: str = "") -> None:
        """Add loose refs below ``base`` (e.g. ``.git/refs/heads``) to ``refs``, named relative to it.

        A loose ref replaces a packed one of the same name, as in git, whether or not it resolves.
        """
        try:
            entries = self.tree.list(*base)
        except FsError as exc:
            if exc.code in INTEGRITY_CODES:
                raise GitStateError(exc.code, exc.message) from exc
            if exc.code == "NOT_FOUND" and not prefix:
                return
            if not prefix:
                raise _metadata_error(exc) from exc
            self._unknown(
                refs, prefix.rstrip("/"), f"ref directory {prefix.rstrip('/')} cannot be listed: {exc.message}"
            )
            return
        for entry in entries:
            name = f"{prefix}{entry.name}"
            self.count += 1
            if self.count > _MAX_REFS:
                raise GitStateError("GIT_METADATA_TOO_LARGE", f"more than {_MAX_REFS} refs")
            if entry.kind == "directory":
                self.loose((*base, entry.name), refs, f"{name}/")
                continue
            if entry.kind != "file":
                # A link or special file may stand for a ref or for a whole directory of refs.
                self._unresolved(refs, name, f"is a {entry.kind}; not followed")
                self._unknown(refs, name, f"ref entry {name} is a {entry.kind}; refs below it are unknown")
                continue
            try:
                value = _text(self.tree, (*base, entry.name), _REF_BYTES)
            except FsError as exc:
                if exc.code in INTEGRITY_CODES:
                    raise GitStateError(exc.code, exc.message) from exc
                self._unresolved(refs, name, f"is unreadable: {exc.message}")
                continue
            except GitStateError as exc:
                self._unresolved(refs, name, f"is unreadable: {exc.message}")
                continue
            stripped = value.strip()
            if stripped.startswith("ref: "):
                self._unresolved(refs, name, f"is a symbolic ref to {stripped[5:]!r}; not followed")
            elif not is_object_id(stripped) or stripped + "\n" not in (value, value + "\n"):
                self._unresolved(refs, name, "has a malformed value")
            elif not is_branch(name):
                self._unresolved(refs, name, "has an invalid name")
            else:
                refs.unresolved.pop(name, None)
                refs.resolved[name] = stripped

    def packed(self) -> list[tuple[str, str]]:
        try:
            text = _text(self.tree, (".git", "packed-refs"), _PACKED_BYTES)
        except FsError as exc:
            if exc.code == "NOT_FOUND":
                return []
            raise _metadata_error(exc) from exc
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

    def _unresolved(self, refs: RefSet, name: str, why: str) -> None:
        refs.resolved.pop(name, None)
        refs.unresolved[name] = why
        self.problems.append(f"ref {name} {why}")

    def _unknown(self, refs: RefSet, prefix: str, why: str) -> None:
        refs.unknown = tuple(sorted({*refs.unknown, prefix}))
        self.problems.append(why)


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
            raise _metadata_error(exc) from exc
        if reftable or (storage is not None and storage != "files"):
            raise GitStateError("CHECKOUT_LAYOUT_UNSUPPORTED", "the reftable ref backend is not supported")

        reader = _Reader(tree)
        local, remote = RefSet(), RefSet()
        for ref, sha in reader.packed():
            if ref.startswith("refs/heads/"):
                name = ref[len("refs/heads/") :]
                if is_branch(name):
                    local.resolved[name] = sha
                else:
                    reader._unresolved(local, name, "has an invalid name (packed)")
            elif ref.startswith("refs/remotes/"):
                name = ref[len("refs/remotes/") :]
                if name.partition("/")[2] and name.partition("/")[2] != "HEAD":
                    if is_branch(name):
                        remote.resolved[name] = sha
                    else:
                        reader._unresolved(remote, name, "has an invalid name (packed)")
        reader.loose((".git", "refs", "heads"), local)
        reader.loose((".git", "refs", "remotes"), remote)
        for name in [n for n in remote.unresolved if n.endswith("/HEAD") and n.count("/") == 1]:
            # refs/remotes/<remote>/HEAD is git's own symbolic ref to the default branch.
            del remote.unresolved[name]

        try:
            head_kind, head = _head(_text(tree, (".git", "HEAD"), _REF_BYTES))
        except FsError as exc:
            raise _metadata_error(exc) from exc
        if head_kind == UNKNOWN:
            reader.problems.append("HEAD is neither a branch reference nor an object id")

        worktrees: list[Worktree] = []
        try:
            entries = tree.list(".git", "worktrees")
        except FsError as exc:
            if exc.code != "NOT_FOUND":
                raise _metadata_error(exc) from exc
            entries = []
        for entry in entries:
            if entry.kind != "directory":
                reader.problems.append(f"worktree entry {entry.name} is a {entry.kind}; its branch is unknown")
                worktrees.append(Worktree(entry.name, None, None, UNKNOWN))
                continue
            try:
                state, branch = _head(_text(tree, (".git", "worktrees", entry.name, "HEAD"), _REF_BYTES))
            except FsError as exc:
                if exc.code in INTEGRITY_CODES:
                    raise GitStateError(exc.code, exc.message) from exc
                state, branch = UNKNOWN, None
                reader.problems.append(f"worktree {entry.name} HEAD is unreadable: {exc.message}")
            except GitStateError as exc:
                state, branch = UNKNOWN, None
                reader.problems.append(f"worktree {entry.name} HEAD is unreadable: {getattr(exc, 'message', exc)}")
            else:
                if state == UNKNOWN:
                    reader.problems.append(f"worktree {entry.name} HEAD is neither a branch reference nor an object id")
            try:
                gitdir = _text(tree, (".git", "worktrees", entry.name, "gitdir"), _REF_BYTES).strip()
                location: str | None = gitdir.removesuffix("/.git") if gitdir else None
            except FsError as exc:
                if exc.code in INTEGRITY_CODES:
                    raise GitStateError(exc.code, exc.message) from exc
                location = None
            except GitStateError:
                location = None
            worktrees.append(Worktree(entry.name, location, branch, state))

        try:
            # Everything above must describe one moment: no observed file, listing or absence changed.
            tree.verify()
        except FsError as exc:
            raise GitStateError(exc.code, f"git metadata changed while it was read: {exc.message}") from exc

        def ordered(refs: RefSet) -> RefSet:
            return RefSet(dict(sorted(refs.resolved.items())), dict(sorted(refs.unresolved.items())), refs.unknown)

        return CheckoutState(
            path=str(tree.root),
            head_branch=head,
            local=ordered(local),
            remote=ordered(remote),
            worktrees=tuple(worktrees),
            origin_url=origin,
            head_known=head_kind != UNKNOWN,
            problems=tuple(sorted(set(reader.problems))),
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
