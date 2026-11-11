from __future__ import annotations

from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from factory.documents import DocumentError, parse_json, parse_yaml, resolve_inside, split_front_matter
from factory.graph import cycle_path, find_cycles, strongly_connected_components
from factory.schemas.validation import SCHEMA_FILES, load_schema, validate_document
from factory.semver import VersionError, parse_range, satisfies

# -- documents -----------------------------------------------------------------


def test_yaml_duplicate_keys_are_rejected() -> None:
    with pytest.raises(DocumentError) as info:
        parse_yaml("id: A\nid: B\n")
    assert info.value.code == "INVALID_YAML"
    assert "duplicate key 'id'" in info.value.message
    assert "line 2" in info.value.message


def test_yaml_syntax_error_reports_location() -> None:
    with pytest.raises(DocumentError) as info:
        parse_yaml("a: [1, 2\nb: 3\n")
    assert info.value.code == "INVALID_YAML"
    assert "line" in info.value.message


def test_yaml_refuses_python_object_tags() -> None:
    with pytest.raises(DocumentError):
        parse_yaml("!!python/object/apply:os.system ['true']\n")


def test_json_duplicate_keys_and_non_finite_numbers_are_rejected() -> None:
    with pytest.raises(DocumentError, match="duplicate key"):
        parse_json('{"a": 1, "a": 2}')
    with pytest.raises(DocumentError, match="non-finite"):
        parse_json('{"a": NaN}')
    with pytest.raises(DocumentError, match="line 1"):
        parse_json("{")


def test_front_matter_split() -> None:
    assert split_front_matter("---\na: 1\n---\nbody\n") == "a: 1\n"
    assert split_front_matter("# no front matter\n") is None
    with pytest.raises(DocumentError) as info:
        split_front_matter("---\na: 1\n")
    assert info.value.code == "INVALID_FRONT_MATTER"


def test_resolve_inside_rejects_escapes(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (tmp_path / "secret.txt").write_text("x")
    (root / "link").symlink_to(tmp_path / "secret.txt")
    assert resolve_inside(root, "../secret.txt") is None
    assert resolve_inside(root, "/etc/passwd") is None
    assert resolve_inside(root, "a\\..\\..\\secret.txt") is None
    assert resolve_inside(root, "link") is None
    assert resolve_inside(root, "sub/file.md") == (root / "sub/file.md").resolve()


# -- semver --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("version", "range_text", "expected"),
    [
        ("1.2.0", "^1.2.0", True),
        ("1.9.9", "^1.2.0", True),
        ("2.0.0", "^1.2.0", False),
        ("1.1.9", "^1.2.0", False),
        ("0.2.5", "^0.2.0", True),
        ("0.3.0", "^0.2.0", False),
        ("0.0.3", "^0.0.3", True),
        ("0.0.4", "^0.0.3", False),
        ("1.2.9", "~1.2.0", True),
        ("1.3.0", "~1.2.0", False),
        ("1.4.0", ">=1.2.0 <2.0.0", True),
        ("2.0.0", ">=1.2.0 <2.0.0", False),
        ("1.2.3", "1.2.3", True),
        ("1.2.4", "=1.2.3", False),
    ],
)
def test_version_ranges(version: str, range_text: str, expected: bool) -> None:
    assert satisfies(version, range_text) is expected


@pytest.mark.parametrize("bad", ["", "latest", "1.2", "^1.2", "1.2.3-beta", ">=1.0.0 ^2.0.0", "01.2.3"])
def test_invalid_ranges_are_rejected(bad: str) -> None:
    with pytest.raises(VersionError):
        parse_range(bad)


# -- graph -----------------------------------------------------------------------


def test_scc_and_cycles() -> None:
    graph = {"A": ["B"], "B": ["C"], "C": ["A"], "D": ["A"], "E": ["E"], "F": []}
    assert strongly_connected_components(graph) == [["A", "B", "C"], ["D"], ["E"], ["F"]]
    assert find_cycles(graph) == [["A", "B", "C"], ["E"]]
    assert cycle_path(graph, ["A", "B", "C"]) == ["A", "B", "C", "A"]
    assert cycle_path(graph, ["E"]) == ["E", "E"]


def test_two_independent_cycles_are_both_reported() -> None:
    graph = {"A": ["B"], "B": ["A"], "X": ["Y"], "Y": ["Z"], "Z": ["X"]}
    assert find_cycles(graph) == [["A", "B"], ["X", "Y", "Z"]]


def test_cycle_detection_is_iterative_for_long_chains() -> None:
    size = 5000
    graph = {f"T{i}": [f"T{i + 1}"] for i in range(size)}
    graph[f"T{size}"] = []
    assert find_cycles(graph) == []
    graph[f"T{size}"] = ["T0"]
    assert len(find_cycles(graph)[0]) == size + 1


# -- schemas ---------------------------------------------------------------------


@pytest.mark.parametrize("kind", sorted(SCHEMA_FILES))
def test_schemas_are_valid_draft_2020_12(kind: str) -> None:
    Draft202012Validator.check_schema(load_schema(kind))


def test_schema_issues_carry_field_paths() -> None:
    issues = validate_document(
        "task",
        {
            "schemaVersion": "factory.task/v1",
            "id": "T-1",
            "revision": 0,
            "title": "x",
            "repository": "svc",
            "kind": "spike",
            "specification": {"path": "../outside.md", "sha256": "abc"},
            "acceptance": [],
            "extra": True,
        },
        "factory/tasks/T-1.yaml",
    )
    found = {(i.code, i.field) for i in issues}
    assert found == {
        ("INVALID_VALUE", "revision"),
        ("INVALID_FORMAT", "specification.path"),
        ("INVALID_FORMAT", "specification.sha256"),
        ("TOO_FEW_ITEMS", "acceptance"),
        ("UNKNOWN_FIELD", "extra"),
    }


def test_snapshot_schema_only_accepts_simulated_provenance() -> None:
    document = {
        "schemaVersion": "factory.evidence-snapshot/v1",
        "project": "p",
        "provenance": {"kind": "github"},
        "approvals": [],
        "repositories": {},
    }
    issues = validate_document("evidence", document, "s.json")
    assert [(i.code, i.field) for i in issues] == [("INVALID_VALUE", "provenance.kind")]
