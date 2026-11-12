"""Strict parsing of YAML, JSON and Markdown front matter, plus safe path handling.

Ambiguous input is rejected: duplicate mapping keys, non-finite JSON numbers,
paths that escape the project directory.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Any

import yaml


class DocumentError(Exception):
    """A document could not be parsed. ``field`` is set when the location is known."""

    def __init__(self, code: str, message: str, field: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate keys instead of silently keeping the last one."""


def _construct_mapping(loader: _StrictLoader, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            hash(key)
        except TypeError as exc:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping", node.start_mark, "found unhashable key", key_node.start_mark
            ) from exc
        if key in mapping:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping", node.start_mark, f"found duplicate key {key!r}", key_node.start_mark
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


# Nesting and size bounds checked on the event stream before any node is composed. PyYAML's event
# parser is iterative; composing and constructing are recursive, so a small, deeply nested document
# would otherwise exhaust the interpreter stack. Factory Core's own documents nest fewer than 10
# levels; 64 leaves ample room while staying far below the recursion limit. 200,000 events covers
# any document within the largest file size Factory Core reads as YAML.
MAX_YAML_DEPTH = 64
MAX_YAML_EVENTS = 200_000


def _check_yaml_shape(text: str) -> None:
    depth = 0
    for count, event in enumerate(yaml.parse(text, Loader=yaml.SafeLoader), 1):
        if count > MAX_YAML_EVENTS:
            raise DocumentError("YAML_TOO_COMPLEX", f"more than {MAX_YAML_EVENTS} YAML events")
        if isinstance(event, yaml.MappingStartEvent | yaml.SequenceStartEvent):
            depth += 1
            if depth > MAX_YAML_DEPTH:
                raise DocumentError("YAML_TOO_COMPLEX", f"nested deeper than {MAX_YAML_DEPTH} levels")
        elif isinstance(event, yaml.MappingEndEvent | yaml.SequenceEndEvent):
            depth -= 1


def parse_yaml(text: str) -> Any:
    try:
        _check_yaml_shape(text)
        return yaml.load(text, Loader=_StrictLoader)  # noqa: S506 - strict SafeLoader subclass
    except RecursionError as exc:
        raise DocumentError("YAML_TOO_COMPLEX", "YAML nested too deeply to parse") from exc
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" (line {mark.line + 1}, column {mark.column + 1})" if mark is not None else ""
        problem = getattr(exc, "problem", None) or str(exc)
        raise DocumentError("INVALID_YAML", f"invalid YAML{where}: {problem}") from exc


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DocumentError("INVALID_JSON", f"duplicate key {key!r}")
        result[key] = value
    return result


def _reject_constant(name: str) -> Any:
    raise DocumentError("INVALID_JSON", f"non-finite number {name} is not allowed")


def parse_json(text: str) -> Any:
    try:
        return json.loads(text, object_pairs_hook=_reject_duplicate_keys, parse_constant=_reject_constant)
    except RecursionError as exc:
        raise DocumentError("INVALID_JSON", "JSON nested too deeply to parse") from exc
    except json.JSONDecodeError as exc:
        raise DocumentError("INVALID_JSON", f"invalid JSON (line {exc.lineno}, column {exc.colno}): {exc.msg}") from exc


def split_front_matter(text: str) -> str | None:
    """Return the YAML front matter of a Markdown document, or None if it has none."""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        return None
    for index in range(1, len(lines)):
        if lines[index].rstrip("\r\n") == "---":
            return "".join(lines[1:index])
    raise DocumentError("INVALID_FRONT_MATTER", "front matter starts with '---' but has no closing '---' line")


def read_text(path: Path) -> str:
    try:
        return path.read_bytes().decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DocumentError("INVALID_ENCODING", f"file is not valid UTF-8: {exc.reason}") from exc


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def resolve_inside(root: Path, relative: str) -> Path | None:
    """Resolve a project-relative POSIX path, or return None if it would escape ``root``."""
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts or "\\" in relative:
        return None
    candidate = (root / Path(*pure.parts)).resolve()
    if not candidate.is_relative_to(root.resolve()):
        return None
    return candidate


def relative_name(root: Path, path: Path) -> str:
    """Project-relative display name, computed lexically so symbolic links cannot make it fail."""
    return path.relative_to(root).as_posix()
