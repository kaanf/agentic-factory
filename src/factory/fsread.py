"""Read-only file access confined to one directory tree, with identity-bound observations.

Used for everything Stage 5 reads outside the project directory: run records under the runtime state
directory, git metadata and Archon configuration in a local checkout, and Archon's home. Those trees
are writable by other processes. The contract has three parts.

**Root acquisition.** The root is acquired by one walk of its configured path, from ``/``, each
component opened relative to the descriptor of the one before (relative paths are anchored at the
working directory). There is no resolve-then-open step and no restart from ``/``. A symbolic link in
the configured path is followed only if root controls it (the link and its directory are owned by
root and the directory is not group- or world-writable) and its target is relative, as for ``/tmp``,
``/var`` and ``/etc`` on macOS or merged ``/usr`` on Linux; any other link is refused
(``ROOT_PATH_SYMLINK_UNTRUSTED``, ``ROOT_PATH_SYMLINK_UNSUPPORTED``); at most 40 are followed. Around
each lookup the parent's modification and status-change times and the entry's device and inode must
not change (``ROOT_CHANGED_DURING_ACQUISITION``). Every directory the walk looks inside must satisfy
the permission policy (``ROOT_PATH_UNSAFE``).

**Permission policy** (applied to every directory and file actually opened, root path and below):
the object is owned by root or the current user; it is not group- or world-writable, except a
directory with the sticky bit; and no access control list grants another principal a modifying
right. Group membership is never used to establish trust: whether a group contains only the current
user cannot be established authoritatively (implicit groups such as macOS ``everyone``, nested and
directory-service groups), so a group-writable object is refused. ACLs: on macOS every extended entry
is read through libc and any *allow* entry with a modifying right (write, append, add file or
subdirectory, delete, delete child, write attributes, write extended attributes, write security,
change owner) is refused, whoever it names; *deny* entries only restrict. On Linux, POSIX access ACLs
are bounded by the group-class mode bits (the ACL mask), which the mode check already covers;
NFSv4 or rich ACLs cannot be evaluated and are refused. On other platforms, or when an ACL cannot be
read, the object is refused (``PERMISSIONS_UNVERIFIABLE``). Mode bits alone are never taken as proof.

**Identity-bound observation.** Below the root every lookup is descriptor-relative with
``O_NOFOLLOW``, so symbolic links are refused, never followed. Every object observed through one
``ConfinedTree`` (listed entries, traversed directories, files read, entries found absent) is
recorded with its device, inode and type; any later open of the same path must find the same object
(``IDENTITY_CHANGED``), so a listed child cannot be replaced before it is read. A listing is complete
only if the directory did not change while it was enumerated (``DIRECTORY_CHANGED``); a file must
not change while it is read (``FILE_CHANGED``). ``verify()`` re-checks every observation at the end of
a logical read: listed directories unchanged, files unchanged, absent entries still absent
(``TREE_CHANGED``). Callers that need a coherent snapshot call it before trusting what they read.
Work is bounded per tree (entries observed, bytes read, ``BUDGET_EXCEEDED``). Each of these codes is
in ``INTEGRITY_CODES``: it means the observation as a whole is unusable, never that something is
absent.

A process running as the current user can change the tree before a component is first observed;
that is the operator's own principal, addressed by running agents as another OS user (4B P2).
Nothing here creates, changes or removes anything.

Requires directory-relative ``open``, ``stat`` and ``readlink`` and ``O_DIRECTORY``/``O_NOFOLLOW``;
platforms without them are refused (``UNSUPPORTED_PLATFORM``).
"""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_REQUIRED = (os.open, os.stat, os.readlink)
_MAX_SYMLINKS = 40  # as Linux's MAXSYMLINKS

# Default budgets per tree. Generous for git metadata (Factory Core reads at most 100,000 refs) while
# keeping memory and time bounded; callers with smaller trees pass tighter ones (runs.py).
DEFAULT_MAX_ENTRIES = 250_000
DEFAULT_MAX_BYTES = 64 * 1024 * 1024

INTEGRITY_CODES: frozenset[str] = frozenset(
    {
        "IDENTITY_CHANGED",
        "DIRECTORY_CHANGED",
        "FILE_CHANGED",
        "TREE_CHANGED",
        "BUDGET_EXCEEDED",
        "UNSAFE_PERMISSIONS",
        "PERMISSIONS_UNVERIFIABLE",
    }
)


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


# -- permission policy ---------------------------------------------------------------------------

# macOS <sys/acl.h>: ACL_TYPE_EXTENDED, entry tags, and the permissions that modify an object.
# Verified against real ACLs (chmod +a) on macOS; see tests/unit/test_fsread_policy.py.
_ACL_TYPE_EXTENDED = 0x00000100
_ACL_EXTENDED_ALLOW = 1
_ACL_FIRST_ENTRY, _ACL_NEXT_ENTRY = 0, -1
_DARWIN_MODIFYING = (
    1 << 2,  # ACL_WRITE_DATA / ACL_ADD_FILE
    1 << 4,  # ACL_DELETE
    1 << 5,  # ACL_APPEND_DATA / ACL_ADD_SUBDIRECTORY
    1 << 6,  # ACL_DELETE_CHILD
    1 << 8,  # ACL_WRITE_ATTRIBUTES
    1 << 10,  # ACL_WRITE_EXTATTRIBUTES
    1 << 12,  # ACL_WRITE_SECURITY
    1 << 13,  # ACL_CHANGE_OWNER
)
_LINUX_UNEVALUATED_ACLS = ("system.nfs4_acl", "system.nfs4acl", "system.richacl")


def _darwin_libc() -> ctypes.CDLL | None:
    name = ctypes.util.find_library("c")
    if name is None:
        return None
    libc = ctypes.CDLL(name, use_errno=True)
    libc.acl_get_fd_np.restype = ctypes.c_void_p
    libc.acl_get_fd_np.argtypes = [ctypes.c_int, ctypes.c_int]
    libc.acl_get_entry.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)]
    libc.acl_get_tag_type.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
    libc.acl_get_permset.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    libc.acl_get_perm_np.argtypes = [ctypes.c_void_p, ctypes.c_int]
    libc.acl_free.argtypes = [ctypes.c_void_p]
    return libc


_LIBC: list[ctypes.CDLL | None] = []


def _darwin_acl(fd: int) -> str:
    if not _LIBC:
        try:
            _LIBC.append(_darwin_libc())
        except (OSError, AttributeError):
            _LIBC.append(None)
    libc = _LIBC[0]
    if libc is None:
        return "unverifiable"
    acl = libc.acl_get_fd_np(fd, _ACL_TYPE_EXTENDED)
    if not acl:
        return "none" if ctypes.get_errno() == errno.ENOENT else "unverifiable"
    try:
        entry = ctypes.c_void_p()
        which = _ACL_FIRST_ENTRY
        while libc.acl_get_entry(acl, which, ctypes.byref(entry)) == 0:
            which = _ACL_NEXT_ENTRY
            tag = ctypes.c_int()
            permset = ctypes.c_void_p()
            if libc.acl_get_tag_type(entry, ctypes.byref(tag)) != 0:
                return "unverifiable"
            if tag.value != _ACL_EXTENDED_ALLOW:
                continue  # deny entries only restrict
            if libc.acl_get_permset(entry, ctypes.byref(permset)) != 0:
                return "unverifiable"
            for permission in _DARWIN_MODIFYING:
                granted = libc.acl_get_perm_np(permset, permission)
                if granted < 0:
                    return "unverifiable"
                if granted:
                    return "unsafe"
        return "none"
    finally:
        libc.acl_free(acl)


def _linux_acl(fd: int) -> str:
    try:
        names = os.listxattr(fd)
    except OSError as exc:
        return "none" if exc.errno in (errno.ENOTSUP, errno.EOPNOTSUPP) else "unverifiable"
    if any(name in _LINUX_UNEVALUATED_ACLS for name in names):
        return "unverifiable"
    # system.posix_acl_access entries for other principals are bounded by the ACL mask, which is the
    # group-class mode bits: the mode check refuses any write there.
    return "none"


def _acl_state(fd: int) -> str:
    """'none' (no ACL grants another principal a modifying right), 'unsafe' or 'unverifiable'."""
    if sys.platform == "darwin":
        return _darwin_acl(fd)
    if sys.platform.startswith("linux"):
        return _linux_acl(fd)
    return "unverifiable"


def _trusted_owner(uid: int) -> bool:
    return uid in (0, os.geteuid())


def _policy(fd: int, info: os.stat_result, what: str, code: str) -> None:
    """Refuse an object another principal could modify. ``code`` names the refusal."""
    mode = stat.S_IMODE(info.st_mode)
    if not _trusted_owner(info.st_uid):
        raise FsError(code, f"{what} is owned by uid {info.st_uid}, not root or the current user")
    if info.st_mode & 0o022 and not (stat.S_ISDIR(info.st_mode) and info.st_mode & stat.S_ISVTX):
        raise FsError(code, f"{what} has mode {mode:o}; group or other users can modify it")
    acl = _acl_state(fd)
    if acl == "unsafe":
        raise FsError(code, f"{what} has an access control list that lets another principal modify it")
    if acl != "none":
        raise FsError("PERMISSIONS_UNVERIFIABLE", f"{what}: its access control list cannot be evaluated here")


def _check_directory(fd: int, text: str) -> os.stat_result:
    """Refuse a directory on the root path that another principal could modify."""
    info = os.fstat(fd)
    _policy(fd, info, f"{text}: a directory on the path", "ROOT_PATH_UNSAFE")
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
            if before.st_mode & 0o022 and not _trusted_owner(entry.st_uid):
                raise FsError("ROOT_PATH_UNSAFE", f"{text}: {part!r} in a shared directory belongs to another user")
            if stat.S_ISLNK(entry.st_mode):
                if entry.st_uid != 0 or before.st_uid != 0 or before.st_mode & 0o022:
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


@dataclass(frozen=True)
class _Seen:
    """One observation: what was at a path (``kind`` None: nothing) and, for files and listed
    directories, the times and size that must still hold at ``verify``."""

    kind: str | None
    device: int = 0
    inode: int = 0
    mtime: int = 0
    ctime: int = 0
    size: int = 0


def _seen(info: os.stat_result) -> _Seen:
    return _Seen(_kind(info.st_mode), info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns, info.st_size)


class ConfinedTree:
    """An acquired root and the observations made through it; use as a context manager."""

    def __init__(
        self, root: Path, fd: int, max_entries: int = DEFAULT_MAX_ENTRIES, max_bytes: int = DEFAULT_MAX_BYTES
    ) -> None:
        self.root = root
        self._fd = fd
        self._max_entries = max_entries
        self._max_bytes = max_bytes
        self._bytes = 0
        self._seen: dict[tuple[str, ...], _Seen] = {(): _seen(os.fstat(fd))}
        self._listed: dict[tuple[str, ...], _Seen] = {}

    @classmethod
    def open(
        cls, path: Path, *, max_entries: int = DEFAULT_MAX_ENTRIES, max_bytes: int = DEFAULT_MAX_BYTES
    ) -> ConfinedTree:
        """Acquire ``path`` as the root (see the module docstring)."""
        if not all(function in os.supports_dir_fd for function in _REQUIRED) or not hasattr(os, "O_DIRECTORY"):
            raise FsError("UNSUPPORTED_PLATFORM", "this platform has no directory-relative file operations")
        text = os.fspath(path)
        if not text or "\0" in text:
            raise FsError("UNSAFE_PATH", f"unusable path {text!r}")
        fd, names = _acquire(text)
        return cls(Path("/" + "/".join(names)), fd, max_entries, max_bytes)

    # -- bookkeeping ------------------------------------------------------------------------------

    def _show(self, parts: tuple[str, ...]) -> str:
        return str(self.root.joinpath(*parts)) if parts else str(self.root)

    def _record(self, parts: tuple[str, ...], seen: _Seen) -> None:
        """Record an observation, or raise if it contradicts an earlier one of the same path."""
        earlier = self._seen.get(parts)
        if earlier is None:
            if len(self._seen) + len(self._listed) >= self._max_entries:
                raise FsError("BUDGET_EXCEEDED", f"more than {self._max_entries} objects observed under {self.root}")
            self._seen[parts] = seen
        elif (earlier.kind, earlier.device, earlier.inode) != (seen.kind, seen.device, seen.inode):
            raise FsError("IDENTITY_CHANGED", f"{self._show(parts)} is not the object observed earlier")

    def _walk(self, parts: tuple[str, ...]) -> int:
        """Open the directory ``parts``; every step must be the object recorded for it. Caller closes."""
        fd = os.dup(self._fd)
        try:
            for index, part in enumerate(parts):
                _check_part(part)
                prefix = parts[: index + 1]
                try:
                    entry = os.stat(part, dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError as exc:
                    self._record(prefix, _Seen(None))
                    raise FsError("NOT_FOUND", f"{self._show(prefix)} does not exist") from exc
                except OSError as exc:
                    raise FsError("UNREADABLE", f"{self._show(prefix)} is not readable: {exc.strerror}") from exc
                self._record(prefix, _seen(entry))
                if stat.S_ISLNK(entry.st_mode):
                    raise FsError("SYMLINK", f"{self._show(prefix)} is a symbolic link; it is not followed")
                if not stat.S_ISDIR(entry.st_mode):
                    raise FsError("NOT_A_DIRECTORY", f"{self._show(prefix)} is not a directory")
                try:
                    child = os.open(part, _DIR_FLAGS, dir_fd=fd)
                except OSError as exc:
                    if exc.errno in (errno.ENOENT, errno.ELOOP, errno.ENOTDIR):
                        # lstat just found a directory here: it was removed or replaced since.
                        raise FsError("IDENTITY_CHANGED", f"{self._show(prefix)} changed while it was opened") from exc
                    raise FsError("UNREADABLE", f"{self._show(prefix)} is not readable: {exc.strerror}") from exc
                os.close(fd)
                fd = child
                info = os.fstat(fd)
                if (info.st_dev, info.st_ino) != (entry.st_dev, entry.st_ino):
                    raise FsError("IDENTITY_CHANGED", f"{self._show(prefix)} changed while it was opened")
                _policy(fd, info, self._show(prefix), "UNSAFE_PERMISSIONS")
        except BaseException:
            os.close(fd)
            raise
        return fd

    # -- observations -----------------------------------------------------------------------------

    def kind(self, *parts: str) -> str | None:
        """The type of the entry at ``parts`` without following a final link, or None if absent."""
        if not parts:
            return "directory"
        fd = self._walk(parts[:-1])
        try:
            _check_part(parts[-1])
            try:
                info = os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                self._record(parts, _Seen(None))
                return None
            except OSError as exc:
                raise FsError("UNREADABLE", f"{self._show(parts)} is not readable: {exc.strerror}") from exc
            self._record(parts, _seen(info))
            return _kind(info.st_mode)
        finally:
            os.close(fd)

    def list(self, *parts: str) -> list[Entry]:
        """Entries of the directory at ``parts``, sorted by name. Raises unless the listing is complete."""
        fd = self._walk(parts)
        try:
            before = os.fstat(fd)
            entries = []
            with os.scandir(fd) as iterator:
                for item in iterator:
                    child = (*parts, item.name)
                    try:
                        info = os.stat(item.name, dir_fd=fd, follow_symlinks=False)
                    except FileNotFoundError as exc:
                        raise FsError("DIRECTORY_CHANGED", f"{self._show(child)} vanished while listed") from exc
                    self._record(child, _seen(info))
                    entries.append(Entry(item.name, _kind(info.st_mode)))
            after = os.fstat(fd)
            if (before.st_mtime_ns, before.st_ctime_ns) != (after.st_mtime_ns, after.st_ctime_ns):
                raise FsError("DIRECTORY_CHANGED", f"{self._show(parts)} changed while it was listed")
            self._listed.setdefault(parts, _seen(after))
            return sorted(entries)
        except OSError as exc:
            raise FsError("UNREADABLE", f"{self._show(parts)} is not readable: {exc.strerror}") from exc
        finally:
            os.close(fd)

    def open_file(self, *parts: str) -> int:
        """A read-only descriptor for the regular file at ``parts``, checked against any earlier
        observation and the permission policy. The caller closes it."""
        if not parts:
            raise FsError("UNSAFE_PATH", "no file name given")
        directory = self._walk(parts[:-1])
        try:
            _check_part(parts[-1])
            try:
                fd = os.open(parts[-1], _FILE_FLAGS, dir_fd=directory)
            except FileNotFoundError as exc:
                self._record(parts, _Seen(None))
                raise FsError("NOT_FOUND", f"{self._show(parts)} does not exist") from exc
            except OSError as exc:
                try:
                    if stat.S_ISLNK(os.stat(parts[-1], dir_fd=directory, follow_symlinks=False).st_mode):
                        raise FsError("SYMLINK", f"{self._show(parts)} is a symbolic link; it is not followed") from exc
                except OSError:
                    pass
                raise FsError("UNREADABLE", f"{self._show(parts)} is not readable: {exc.strerror}") from exc
        finally:
            os.close(directory)
        try:
            info = os.fstat(fd)
            self._record(parts, _seen(info))
            if not stat.S_ISREG(info.st_mode):
                raise FsError("NOT_A_FILE", f"{self._show(parts)} is not a regular file")
            _policy(fd, info, self._show(parts), "UNSAFE_PERMISSIONS")
        except BaseException:
            os.close(fd)
            raise
        return fd

    def read(self, *parts: str, max_bytes: int, single_link: bool = False) -> bytes:
        """The bytes of the regular file at ``parts``; it must not change while it is read."""
        fd = self.open_file(*parts)
        try:
            info = os.fstat(fd)
            if single_link and info.st_nlink != 1:
                raise FsError("HARD_LINKED", f"{self._show(parts)} has {info.st_nlink} hard links; it is not trusted")
            if info.st_size > max_bytes:
                raise FsError("TOO_LARGE", f"{self._show(parts)} is larger than {max_bytes} bytes")
            if self._bytes + info.st_size > self._max_bytes:
                raise FsError("BUDGET_EXCEEDED", f"more than {self._max_bytes} bytes read under {self.root}")
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
            after = os.fstat(fd)
            if _seen(after) != _seen(info) or len(data) != info.st_size:
                raise FsError("FILE_CHANGED", f"{self._show(parts)} changed while it was read")
            self._bytes += len(data)
            self._seen[parts] = _seen(after)
            return data
        except OSError as exc:
            raise FsError("UNREADABLE", f"{self._show(parts)} is not readable: {exc.strerror}") from exc
        finally:
            os.close(fd)

    def verify(self) -> None:
        """Raise ``TREE_CHANGED`` (or ``IDENTITY_CHANGED``) unless every observation still holds:
        listed directories unchanged, files unchanged, absent entries still absent."""
        for parts, listed in sorted(self._listed.items()):
            fd = self._walk(parts)
            try:
                now = os.fstat(fd)
            finally:
                os.close(fd)
            if (now.st_mtime_ns, now.st_ctime_ns) != (listed.mtime, listed.ctime):
                raise FsError("TREE_CHANGED", f"{self._show(parts)} changed after it was listed")
        for parts, seen in sorted(self._seen.items()):
            if not parts or seen.kind == "directory":
                continue  # identities of directories are re-checked by every walk above
            try:
                fd = self._walk(parts[:-1])
            except FsError as exc:
                raise FsError("TREE_CHANGED", f"{self._show(parts)}: {exc.message}") from exc
            try:
                try:
                    now_info = os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)
                    now = _seen(now_info)
                except FileNotFoundError:
                    now = _Seen(None)
            finally:
                os.close(fd)
            same = (
                now == seen
                if seen.kind == "file"
                else (now.kind, now.device, now.inode) == (seen.kind, seen.device, seen.inode)
            )
            if not same:
                raise FsError("TREE_CHANGED", f"{self._show(parts)} changed after it was observed")

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
