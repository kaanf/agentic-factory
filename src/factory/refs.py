"""Strict validation of values that become git refs, object ids, path components or command arguments.

Every check uses ``fullmatch`` or explicit comparisons: with ``match`` or ``search``, ``$`` also
accepts a trailing newline, and JSON Schema ``pattern`` uses search semantics. Values read from
manifests, task files and run records are untrusted data; anything these functions reject is never
placed in a path, a request or a command.
"""

from __future__ import annotations

import re
from typing import Any

_SHA1 = re.compile(r"[0-9a-f]{40}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
# Characters and sequences git forbids anywhere in a ref name (git check-ref-format rules 3-5, 8-9).
_REFNAME_FORBIDDEN = re.compile(r"[\x00-\x20\x7f~^:?*\[\\]|\.\.|@\{|//")
# Task ids as the task schema defines them, matched in full.
_TASK_ID = re.compile(r"[A-Za-z0-9]+(?:[._-][A-Za-z0-9]+)*")
_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
# Names of workflows, providers and assistants: no option-like or path-like forms.
_NAME = re.compile(r"[a-z0-9]+(?:[._-][a-z0-9]+)*")
_VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-[0-9A-Za-z.-]+)?")
# The characters manifest branch names may use; feature branches are built only from them.
_COMMAND_BRANCH = re.compile(r"[A-Za-z0-9._/-]{1,200}")
# C0 controls other than tab and newline, DEL, C1 controls, and bidirectional/zero-width formatting
# characters that can make displayed text differ from the argument actually passed.
_UNSAFE_TEXT = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]")


def is_sha(value: Any) -> bool:
    """A full 40-hex SHA-1 object id."""
    return isinstance(value, str) and _SHA1.fullmatch(value) is not None


def is_object_id(value: Any) -> bool:
    """A full SHA-1 or SHA-256 object id."""
    return isinstance(value, str) and (_SHA1.fullmatch(value) is not None or _SHA256.fullmatch(value) is not None)


def is_branch(value: Any) -> bool:
    """Whether ``value`` is a name git accepts for a branch (``git check-ref-format --branch``)."""
    return (
        isinstance(value, str)
        and value not in ("", "@", "HEAD")
        and _REFNAME_FORBIDDEN.search(value) is None
        and not value.startswith(("/", "-"))
        and not value.endswith(("/", ".", ".lock"))
        and not any(part.startswith(".") or part.endswith(".lock") for part in value.split("/"))
    )


def is_command_branch(value: Any) -> bool:
    """A branch that is also safe as a command-line value: only the manifest's branch characters, and no
    component that could be read as an option."""
    return (
        is_branch(value)
        and _COMMAND_BRANCH.fullmatch(value) is not None
        and not any(part.startswith("-") for part in value.split("/"))
    )


def is_task_id(value: Any) -> bool:
    return isinstance(value, str) and len(value) <= 80 and _TASK_ID.fullmatch(value) is not None


def is_slug(value: Any) -> bool:
    return isinstance(value, str) and len(value) <= 64 and _SLUG.fullmatch(value) is not None


def is_name(value: Any) -> bool:
    """A workflow, provider or assistant name: lower-case words joined by '.', '_' or '-'."""
    return isinstance(value, str) and len(value) <= 100 and _NAME.fullmatch(value) is not None


def is_version(value: Any) -> bool:
    """A plain semantic version, without build metadata or a leading 'v'."""
    return isinstance(value, str) and len(value) <= 64 and _VERSION.fullmatch(value) is not None


def unsafe_text(value: str) -> str | None:
    """Return the first character that must not appear in a command argument or display, else None."""
    match = _UNSAFE_TEXT.search(value)
    return match.group(0) if match else None
