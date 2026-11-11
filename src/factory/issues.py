"""Validation issues reported with the file and field they refer to."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, order=True)
class Issue:
    """One validation problem.

    ``file`` is a project-relative POSIX path ("" for project-level problems).
    ``field`` is a dotted path into the document, e.g. ``dependsOn[0].task``.
    """

    file: str
    field: str
    code: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "field": self.field, "file": self.file, "message": self.message}

    def render(self) -> str:
        location = self.file or "<project>"
        if self.field:
            location = f"{location}: {self.field}"
        return f"{location}: [{self.code}] {self.message}"


def join_field(parent: str, part: str | int) -> str:
    if isinstance(part, int):
        return f"{parent}[{part}]"
    return f"{parent}.{part}" if parent else part
