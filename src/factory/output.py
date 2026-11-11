"""Writes an evidence snapshot to a file outside the project directory, never through an existing file.

Used by ``evidence collect --output`` and ``plan --live --save-evidence``. Collection takes time,
so the destination is checked before collection and written after it. To keep the write from being
redirected in between:

- Before collection, the destination's directory is resolved (no symbolic links remain), refused if
  it or the destination is inside the project, and opened. The open directory descriptor pins that
  directory; every later operation is relative to it.
- An existing destination must be a regular file. A symbolic link, directory or other file type is
  refused, so a link that points into the project is never followed.
- The snapshot is written to a new, uniquely named temporary file in that directory (created with
  ``O_EXCL | O_NOFOLLOW``), flushed to disk, and renamed over the destination name. ``rename``
  replaces the directory entry: an existing file's contents are never truncated or rewritten, a hard
  link elsewhere (inside the project or not) keeps its old contents, and a symbolic link placed at the
  destination during collection is replaced, not followed.
- Just before the temporary file is created, and again just before the rename, the directory's path
  must still resolve to itself and name the pinned directory. If it was moved, replaced, or a
  component became a symbolic link, nothing is saved.
- On any failure the temporary file is removed, and the destination is left as it was.

Limitation: between the last check and the rename, a process that can write to both the output
directory's parent and the project could still move the output directory into the project. POSIX
offers no portable way to rename only within a directory that is guaranteed to stay outside a given
tree. Platforms without directory-relative file operations are refused rather than served unsafely.
"""

from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path
from types import TracebackType
from typing import Any

from factory.render import to_json

_DIR_FD_FUNCTIONS = (os.open, os.rename, os.unlink, os.stat)


class OutputError(Exception):
    """The snapshot cannot be saved. ``refused`` marks a destination the caller may never use (a usage error)."""

    def __init__(self, message: str, refused: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.refused = refused


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]


class SnapshotOutput:
    """A destination prepared before collection. Use as a context manager so the directory is closed."""

    def __init__(self, path: Path, directory: Path, name: str, directory_fd: int) -> None:
        self.path = path
        self.directory = directory
        self.name = name
        self._fd = directory_fd
        info = os.fstat(directory_fd)
        self._identity = (info.st_dev, info.st_ino)

    @classmethod
    def prepare(cls, path: Path, project_root: Path) -> SnapshotOutput:
        if not all(function in os.supports_dir_fd for function in _DIR_FD_FUNCTIONS):
            raise OutputError(
                "this platform has no directory-relative file operations, so evidence cannot be saved safely"
            )
        name = path.name
        if name in ("", ".", ".."):
            raise OutputError(f"{path} does not name a file", refused=True)
        try:
            directory = Path(os.path.realpath(path.parent, strict=True))
        except OSError as exc:
            raise OutputError(f"directory {path.parent} is not usable: {exc.strerror}") from exc
        if (directory / name).is_relative_to(project_root):
            raise OutputError(f"{path} must not be inside the project directory (it is read-only)", refused=True)
        try:
            fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError as exc:
            raise OutputError(f"directory {directory} is not usable: {exc.strerror}") from exc
        output = cls(path, directory, name, fd)
        try:
            output._confirm_directory()
            output._check_destination()
        except BaseException:
            output.close()
            raise
        return output

    def _confirm_directory(self) -> None:
        """Raise unless the directory's path still resolves to itself and names the pinned directory."""
        try:
            resolved = os.path.realpath(self.directory, strict=True)
            info = os.stat(self.directory)
        except OSError:
            resolved, info = None, None
        if resolved != str(self.directory) or info is None or (info.st_dev, info.st_ino) != self._identity:
            raise OutputError(f"directory {self.directory} was moved or replaced; nothing was saved")

    def _check_destination(self) -> None:
        try:
            info = os.stat(self.name, dir_fd=self._fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise OutputError(f"{self.path} is not usable: {exc.strerror}") from exc
        if stat.S_ISLNK(info.st_mode):
            raise OutputError(f"{self.path} is a symbolic link; refusing to write through or replace it", refused=True)
        if not stat.S_ISREG(info.st_mode):
            raise OutputError(f"{self.path} exists and is not a regular file", refused=True)

    def save(self, document: Any) -> None:
        """Atomically replace the destination entry with ``document`` as canonical JSON."""
        try:
            data = to_json(document).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise OutputError(f"cannot serialize the snapshot: {exc}") from exc
        self._confirm_directory()
        temp = ""
        try:
            for _ in range(10):
                candidate = f".factory-{secrets.token_hex(8)}.tmp"
                try:
                    fd = os.open(
                        candidate,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                        0o666,
                        dir_fd=self._fd,
                    )
                except FileExistsError:
                    continue
                temp = candidate
                break
            else:
                raise OutputError(f"cannot create a temporary file in {self.directory}")
            try:
                _write_all(fd, data)
                os.fsync(fd)
            finally:
                os.close(fd)
            self._confirm_directory()
            os.replace(temp, self.name, src_dir_fd=self._fd, dst_dir_fd=self._fd)
            temp = ""
        except OSError as exc:
            raise OutputError(f"cannot write {self.path}: {exc.strerror or exc}") from exc
        finally:
            if temp:
                try:
                    os.unlink(temp, dir_fd=self._fd)
                except OSError:
                    pass

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1

    def __enter__(self) -> SnapshotOutput:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        self.close()
