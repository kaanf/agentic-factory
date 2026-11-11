"""Read-only file access confined to one directory tree, never following symbolic links.

Used for everything Stage 5 reads outside the project directory: run records under the runtime state
directory, git metadata and Archon configuration in a local checkout, and Archon's install record.
Those trees are writable by other processes, so:

- The root is resolved once and opened as a directory descriptor. Every later lookup is relative to
  that descriptor, one component at a time, with ``O_NOFOLLOW``: a symbolic link anywhere below the
  root is refused, never followed, so nothing outside the root can be reached.
- Path components come from callers as separate strings and are refused if empty, ``.``, ``..`` or
  containing ``/`` or NUL.
- Files must be regular files (a FIFO or device is refused without blocking, thanks to
  ``O_NONBLOCK``), at most ``max_bytes`` long, and optionally have exactly one link: a hard link
  would let a record be changed through a path outside the tree.
- Nothing here creates, changes or removes anything.

Each read is a snapshot of one file at one moment. Reading several files is not atomic; callers that
need consistency across files must compare what they read.
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
_REQUIRED = (os.open, os.stat)


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


class ConfinedTree:
    """An open directory; use as a context manager so the descriptor is closed."""

    def __init__(self, root: Path, fd: int) -> None:
        self.root = root
        self._fd = fd

    @classmethod
    def open(cls, path: Path) -> ConfinedTree:
        """Open ``path`` (symbolic links in ``path`` itself are resolved once) as the root."""
        if not all(function in os.supports_dir_fd for function in _REQUIRED):
            raise FsError("UNSUPPORTED_PLATFORM", "this platform has no directory-relative file operations")
        try:
            resolved = Path(os.path.realpath(path, strict=True))
            fd = os.open(resolved, _DIR_FLAGS)
        except FileNotFoundError as exc:
            raise FsError("NOT_FOUND", f"{path} does not exist") from exc
        except OSError as exc:
            if exc.errno == errno.ENOTDIR:
                raise FsError("NOT_A_DIRECTORY", f"{path} is not a directory") from exc
            raise FsError("UNREADABLE", f"{path} is not readable: {exc.strerror}") from exc
        return cls(resolved, fd)

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
