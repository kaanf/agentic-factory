"""Read-only file access confined to one directory tree, never following symbolic links.

Used for everything Stage 5 reads outside the project directory: run records under the runtime state
directory, git metadata and Archon configuration in a local checkout, and Archon's install record.
Those trees are writable by other processes, so:

- The root is acquired by one walk of its configured path, from ``/``, each component opened
  relative to the descriptor of the one before (relative paths are anchored at the working
  directory). There is no resolve-then-open step and no restart from ``/``. Trust contract:

  * every directory the walk looks inside, the root included, must be modifiable only by root or
    the current user: owned by one of them, and not group- or world-writable unless sticky; inside a
    sticky directory the entry used must be owned by root or the current user (others cannot rename
    it). Another principal therefore cannot swap any component of the path;
  * a symbolic link in the configured path is followed only if root controls it (the link and its
    directory are owned by root and the directory is not group- or world-writable) and its target is
    relative, as for ``/tmp``, ``/var`` and ``/etc`` on macOS or merged ``/usr`` on Linux; any other
    link, and any absolute target, is refused (``ROOT_PATH_SYMLINK_UNTRUSTED``,
    ``ROOT_PATH_SYMLINK_UNSUPPORTED``). At most 40 links are followed;
  * around each lookup, the parent directory must not change (modification and status-change times)
    and the entry found by ``lstat`` must be the directory opened (device and inode); otherwise the
    acquisition is refused (``ROOT_CHANGED_DURING_ACQUISITION``), never retried or redirected.

  The root's displayed path is the sequence of directories actually opened. A process running as the
  current user can still change the path before the walk reaches a component; that is the same
  principal as the operator, and is addressed by running agents as another OS user (4B P2).
- Below the root every lookup is relative to the root's descriptor, one component at a time, with
  ``O_NOFOLLOW``: a symbolic link anywhere below the root is refused, never followed, so nothing
  outside the root can be reached. Once acquired, the root is pinned by its descriptor: renaming or
  replacing its path afterwards does not change which tree is read.
- Path components come from callers as separate strings and are refused if empty, ``.``, ``..`` or
  containing ``/`` or NUL.
- Files must be regular files (a FIFO or device is refused without blocking, thanks to
  ``O_NONBLOCK``), at most ``max_bytes`` long, and optionally have exactly one link: a hard link
  would let a record be changed through a path outside the tree.
- Nothing here creates, changes or removes anything.

Each read is a snapshot of one file at one moment. Reading several files is not atomic; callers that
need consistency across files must compare what they read. A process that can write inside the tree
can still change its contents, including moving another directory into it; confinement is about
which tree is read, not about freezing it.

Requires directory-relative ``open``, ``stat`` and ``readlink`` (``os.supports_dir_fd``) and
``O_DIRECTORY``/``O_NOFOLLOW``; platforms without them are refused (``UNSUPPORTED_PLATFORM``).
"""

from __future__ import annotations

import errno
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_REQUIRED = (os.open, os.stat, os.readlink)
_MAX_SYMLINKS = 40  # as Linux's MAXSYMLINKS


class FsError(Exception):
    """A confined read failed. ``code`` is a stable reason code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, order=True)
class Entry:
    name: str
    kind: str  # "file", "directory", "symlink" or "other"


def _check_part(part: str) -> None:
    if part in ("", ".", "..") or "/" in part or "\0" in part:
        raise FsError("UNSAFE_PATH", f"unsafe path component {part!r}")


def _kind(mode: int) -> str:
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISREG(mode):
        return "file"
    return "other"


def _writable_by_others(info: os.stat_result) -> bool:
    return bool(info.st_mode & 0o022)


def _trusted_owner(uid: int) -> bool:
    return uid in (0, os.geteuid())


def _check_directory(fd: int, text: str) -> os.stat_result:
    """Refuse a directory that a principal other than root or the current user could modify."""
    info = os.fstat(fd)
    if not _trusted_owner(info.st_uid) or (_writable_by_others(info) and not info.st_mode & stat.S_ISVTX):
        raise FsError(
            "ROOT_PATH_UNSAFE",
            f"{text}: a directory on the path can be modified by another user (owner {info.st_uid}, "
            f"mode {stat.S_IMODE(info.st_mode):o})",
        )
    return info


def _acquire(text: str) -> tuple[int, list[str]]:
    """Open the directory ``text`` names under the trust contract in the module docstring.

    Returns the root descriptor and the names of the directories opened from ``/``. Raises ``FsError``.
    """
    if not text.startswith("/"):
        text = os.getcwd().rstrip("/") + "/" + text
    stack: list[tuple[int, str]] = [(os.open("/", _DIR_FLAGS), "")]
    pending = text.split("/")[::-1]  # a stack: next component last
    links = 0
    try:
        while pending:
            part = pending.pop()
            if part in ("", "."):
                continue
            if part == "..":
                if len(stack) > 1:
                    os.close(stack.pop()[0])
                continue
            parent = stack[-1][0]
            before = _check_directory(parent, text)
            try:
                entry = os.stat(part, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError as exc:
                raise FsError("NOT_FOUND", f"{text} does not exist") from exc
            except OSError as exc:
                raise FsError("UNREADABLE", f"{text} is not readable: {exc.strerror}") from exc
            if _writable_by_others(before) and not _trusted_owner(entry.st_uid):
                raise FsError("ROOT_PATH_UNSAFE", f"{text}: {part!r} in a shared directory belongs to another user")
            if stat.S_ISLNK(entry.st_mode):
                if entry.st_uid != 0 or before.st_uid != 0 or _writable_by_others(before):
                    raise FsError(
                        "ROOT_PATH_SYMLINK_UNTRUSTED",
                        f"{text}: {part!r} is a symbolic link not controlled by root; configure the real path",
                    )
                links += 1
                if links > _MAX_SYMLINKS:
                    raise FsError("UNREADABLE", f"{text}: too many levels of symbolic links")
                try:
                    target = os.readlink(part, dir_fd=parent)
                except OSError as exc:
                    raise FsError("UNREADABLE", f"{text} is not readable: {exc.strerror}") from exc
                if not target or "\0" in target or target.startswith("/"):
                    raise FsError(
                        "ROOT_PATH_SYMLINK_UNSUPPORTED",
                        f"{text}: {part!r} has an absolute or unusable target; configure the real path",
                    )
                pending.extend(target.split("/")[::-1])
                continue
            if not stat.S_ISDIR(entry.st_mode):
                raise FsError("NOT_A_DIRECTORY", f"{text} is not a directory")
            try:
                child = os.open(part, _DIR_FLAGS, dir_fd=parent)
            except OSError as exc:
                raise FsError(
                    "ROOT_CHANGED_DURING_ACQUISITION", f"{text}: {part!r} changed while it was opened"
                ) from exc
            opened = os.fstat(child)
            after = os.fstat(parent)
            if (opened.st_dev, opened.st_ino) != (entry.st_dev, entry.st_ino) or (
                after.st_mtime_ns,
                after.st_ctime_ns,
            ) != (before.st_mtime_ns, before.st_ctime_ns):
                os.close(child)
                raise FsError(
                    "ROOT_CHANGED_DURING_ACQUISITION", f"{text}: the path changed while the root was acquired"
                )
            stack.append((child, part))
        _check_directory(stack[-1][0], text)
    except BaseException:
        for fd, _ in stack:
            os.close(fd)
        raise
    names = [name for _, name in stack[1:]]
    root = stack.pop()[0]
    for fd, _ in stack:
        os.close(fd)
    return root, names


class ConfinedTree:
    """An open directory; use as a context manager so the descriptor is closed."""

    def __init__(self, root: Path, fd: int) -> None:
        self.root = root
        self._fd = fd

    @classmethod
    def open(cls, path: Path) -> ConfinedTree:
        """Acquire ``path`` as the root by a descriptor-relative walk (see the module docstring)."""
        if not all(function in os.supports_dir_fd for function in _REQUIRED) or not hasattr(os, "O_DIRECTORY"):
            raise FsError("UNSUPPORTED_PLATFORM", "this platform has no directory-relative file operations")
        text = os.fspath(path)
        if not text or "\0" in text:
            raise FsError("UNSAFE_PATH", f"unusable path {text!r}")
        fd, names = _acquire(text)
        return cls(Path("/" + "/".join(names)), fd)

    def _open_dir(self, parts: tuple[str, ...]) -> int:
        """Open the directory ``parts`` below the root. The caller closes the returned descriptor."""
        fd = os.dup(self._fd)
        try:
            for part in parts:
                _check_part(part)
                try:
                    child = os.open(part, _DIR_FLAGS, dir_fd=fd)
                except FileNotFoundError as exc:
                    raise FsError("NOT_FOUND", f"{self._show(parts)} does not exist") from exc
                except OSError as exc:
                    raise self._classify(exc, fd, part, parts) from exc
                os.close(fd)
                fd = child
        except BaseException:
            os.close(fd)
            raise
        return fd

    def _classify(self, exc: OSError, fd: int, part: str, parts: tuple[str, ...]) -> FsError:
        try:
            info = os.stat(part, dir_fd=fd, follow_symlinks=False)
        except OSError:
            return FsError("UNREADABLE", f"{self._show(parts)} is not readable: {exc.strerror}")
        kind = _kind(info.st_mode)
        if kind == "symlink":
            return FsError("SYMLINK", f"{self._show(parts)} contains a symbolic link; it is not followed")
        if exc.errno in (errno.ENOTDIR, errno.ELOOP):
            return FsError("NOT_A_DIRECTORY", f"{self._show(parts)} contains a non-directory component")
        return FsError("UNREADABLE", f"{self._show(parts)} is not readable: {exc.strerror}")

    def _show(self, parts: tuple[str, ...]) -> str:
        return str(self.root.joinpath(*parts)) if parts else str(self.root)

    def kind(self, *parts: str) -> str | None:
        """The type of the entry at ``parts`` without following a final link, or None if absent."""
        if not parts:
            return "directory"
        fd = self._open_dir(parts[:-1])
        try:
            _check_part(parts[-1])
            try:
                return _kind(os.stat(parts[-1], dir_fd=fd, follow_symlinks=False).st_mode)
            except FileNotFoundError:
                return None
            except OSError as exc:
                raise FsError("UNREADABLE", f"{self._show(parts)} is not readable: {exc.strerror}") from exc
        finally:
            os.close(fd)

    def list(self, *parts: str) -> list[Entry]:
        """Entries of the directory at ``parts``, sorted by name. Link targets are not examined."""
        fd = self._open_dir(parts)
        try:
            entries = []
            with os.scandir(fd) as iterator:
                for item in iterator:
                    try:
                        mode = os.stat(item.name, dir_fd=fd, follow_symlinks=False).st_mode
                    except OSError:
                        entries.append(Entry(item.name, "other"))
                        continue
                    entries.append(Entry(item.name, _kind(mode)))
            return sorted(entries)
        except OSError as exc:
            raise FsError("UNREADABLE", f"{self._show(parts)} is not readable: {exc.strerror}") from exc
        finally:
            os.close(fd)

    def open_file(self, *parts: str) -> int:
        """A read-only descriptor for the regular file at ``parts``. The caller closes it."""
        if not parts:
            raise FsError("UNSAFE_PATH", "no file name given")
        directory = self._open_dir(parts[:-1])
        try:
            _check_part(parts[-1])
            try:
                fd = os.open(parts[-1], _FILE_FLAGS, dir_fd=directory)
            except FileNotFoundError as exc:
                raise FsError("NOT_FOUND", f"{self._show(parts)} does not exist") from exc
            except OSError as exc:
                raise self._classify(exc, directory, parts[-1], parts) from exc
        finally:
            os.close(directory)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise FsError("NOT_A_FILE", f"{self._show(parts)} is not a regular file")
        except BaseException:
            os.close(fd)
            raise
        return fd

    def read(self, *parts: str, max_bytes: int, single_link: bool = False) -> bytes:
        """The bytes of the regular file at ``parts``."""
        fd = self.open_file(*parts)
        try:
            info = os.fstat(fd)
            if single_link and info.st_nlink != 1:
                raise FsError("HARD_LINKED", f"{self._show(parts)} has {info.st_nlink} hard links; it is not trusted")
            if info.st_size > max_bytes:
                raise FsError("TOO_LARGE", f"{self._show(parts)} is larger than {max_bytes} bytes")
            chunks = []
            remaining = max_bytes + 1
            while remaining > 0:
                chunk = os.read(fd, min(remaining, 65536))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
            if len(data) > max_bytes:
                raise FsError("TOO_LARGE", f"{self._show(parts)} grew beyond {max_bytes} bytes while being read")
            return data
        except OSError as exc:
            raise FsError("UNREADABLE", f"{self._show(parts)} is not readable: {exc.strerror}") from exc
        finally:
            os.close(fd)

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1

    def __enter__(self) -> ConfinedTree:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        self.close()
