"""JSON Schema validation that reports issues with file and field locations."""

from __future__ import annotations

import json
from functools import cache
from importlib import resources
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from factory.issues import Issue, join_field

SCHEMA_FILES: dict[str, str] = {
    "project": "project-manifest.v1.schema.json",
    "task": "task.v1.schema.json",
    "profile": "architecture-profile.v1.schema.json",
    "adr": "adr-frontmatter.v1.schema.json",
    "evidence": "evidence-snapshot.v1.schema.json",
}

_CODES: dict[str, str] = {
    "required": "MISSING_FIELD",
    "additionalProperties": "UNKNOWN_FIELD",
    "enum": "INVALID_VALUE",
    "const": "INVALID_VALUE",
    "type": "INVALID_TYPE",
    "pattern": "INVALID_FORMAT",
    "minItems": "TOO_FEW_ITEMS",
    "uniqueItems": "DUPLICATE_ITEM",
    "minimum": "INVALID_VALUE",
    "minLength": "INVALID_VALUE",
    "maxLength": "INVALID_VALUE",
    "propertyNames": "INVALID_FORMAT",
}


@cache
def load_schema(kind: str) -> dict[str, Any]:
    text = resources.files("factory.schemas").joinpath(SCHEMA_FILES[kind]).read_text(encoding="utf-8")
    schema: dict[str, Any] = json.loads(text)
    return schema


@cache
def _validator(kind: str) -> Draft202012Validator:
    schema = load_schema(kind)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _field(path: Any) -> str:
    field = ""
    for part in path:
        field = join_field(field, part)
    return field


def _leaf_errors(error: ValidationError) -> list[ValidationError]:
    # anyOf/oneOf failures carry their real causes in ``context``; report the most specific ones.
    if error.context:
        leaves: list[ValidationError] = []
        for sub in error.context:
            leaves.extend(_leaf_errors(sub))
        return leaves
    return [error]


def _issues_for(error: ValidationError, file: str) -> list[Issue]:
    field = _field(error.absolute_path)
    code = _CODES.get(str(error.validator), "SCHEMA_VIOLATION")
    if error.validator == "required":
        instance = error.instance if isinstance(error.instance, dict) else {}
        missing = [name for name in error.validator_value if name not in instance]
        return [Issue(file, join_field(field, name), code, f"missing required field '{name}'") for name in missing]
    if error.validator == "additionalProperties" and isinstance(error.instance, dict):
        allowed = set(error.schema.get("properties", {}))
        extras = sorted(str(key) for key in error.instance if key not in allowed)
        return [Issue(file, join_field(field, key), code, f"unknown field '{key}'") for key in extras]
    if error.validator == "propertyNames":
        return [Issue(file, field, code, f"invalid key {error.instance!r}")]
    return [Issue(file, field, code, error.message)]


def validate_document(kind: str, document: Any, file: str) -> list[Issue]:
    """Validate ``document`` against the named schema; return issues sorted deterministically."""
    issues: set[Issue] = set()
    for error in _validator(kind).iter_errors(document):
        for leaf in _leaf_errors(error):
            issues.update(_issues_for(leaf, file))
    return sorted(issues)
