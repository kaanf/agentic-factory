"""Minimal semantic-version parsing and range matching for declared output versions.

Supported ranges: exact ``1.2.3``, caret ``^1.2.0``, tilde ``~1.2.0``, and space-separated
comparators such as ``>=1.2.0 <2.0.0``. Pre-release and build metadata are rejected rather than
guessed at.
"""

from __future__ import annotations

import re
from collections.abc import Callable

Version = tuple[int, int, int]

_VERSION = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
_COMPARATOR = re.compile(r"^(>=|<=|>|<|=)?(.+)$")


class VersionError(ValueError):
    pass


def parse_version(text: str) -> Version:
    match = _VERSION.match(text.strip())
    if match is None:
        raise VersionError(f"'{text}' is not a MAJOR.MINOR.PATCH version")
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch


Predicate = Callable[[Version], bool]


def _caret(base: Version) -> Predicate:
    major, minor, patch = base
    if major > 0:
        upper: Version = (major + 1, 0, 0)
    elif minor > 0:
        upper = (0, minor + 1, 0)
    else:
        upper = (0, 0, patch + 1)
    return lambda v: base <= v < upper


def _tilde(base: Version) -> Predicate:
    major, minor, _ = base
    upper: Version = (major, minor + 1, 0)
    return lambda v: base <= v < upper


def _comparator(op: str, base: Version) -> Predicate:
    operations: dict[str, Predicate] = {
        ">=": lambda v: v >= base,
        "<=": lambda v: v <= base,
        ">": lambda v: v > base,
        "<": lambda v: v < base,
        "=": lambda v: v == base,
    }
    return operations[op]


def parse_range(text: str) -> list[Predicate]:
    parts = text.split()
    if not parts:
        raise VersionError("empty version range")
    if len(parts) == 1 and parts[0][0] in "^~":
        base = parse_version(parts[0][1:])
        return [_caret(base) if parts[0][0] == "^" else _tilde(base)]
    predicates: list[Predicate] = []
    for part in parts:
        match = _COMPARATOR.match(part)
        if match is None or match.group(2)[0] in "^~":
            raise VersionError(f"unsupported range element '{part}'")
        predicates.append(_comparator(match.group(1) or "=", parse_version(match.group(2))))
    return predicates


def satisfies(version: str, range_text: str) -> bool:
    parsed = parse_version(version)
    return all(predicate(parsed) for predicate in parse_range(range_text))
