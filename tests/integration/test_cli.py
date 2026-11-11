"""CLI behavior on invalid projects, malformed evidence, exit codes and the read-only guarantee."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from support import SINGLE_REPO, THREE_REPO, copy_project, three_repo_baseline, write_snapshot


def _errors(cli, project: Path) -> list[tuple[str, str, str]]:  # type: ignore[no-untyped-def]
    code, out, _ = cli("validate", project, "--format", "json")
    assert code == 2, out
    return [(e["file"], e["field"], e["code"]) for e in json.loads(out)["errors"]]


def _edit(path: Path, change: Callable[[dict[str, Any]], None]) -> None:
    document = yaml.safe_load(path.read_text())
    change(document)
    path.write_text(yaml.safe_dump(document, sort_keys=False))


@pytest.fixture
def project(tmp_path: Path) -> Path:
    return copy_project(THREE_REPO, tmp_path)


TASKS = "factory/tasks"


def test_duplicate_task_ids(cli, project: Path) -> None:  # type: ignore[no-untyped-def]
    (project / TASKS / "SVC-IMPL-007-copy.yaml").write_text((project / TASKS / "SVC-IMPL-007.yaml").read_text())
    assert _errors(cli, project) == [(f"{TASKS}/SVC-IMPL-007.yaml", "id", "DUPLICATE_TASK_ID")]


def test_self_dependency(cli, project: Path) -> None:  # type: ignore[no-untyped-def]
    _edit(
        project / TASKS / "SVC-IMPL-007.yaml",
        lambda d: d["dependsOn"].append({"task": "SVC-IMPL-007", "gate": "merge", "requires": "merged"}),
    )
    assert _errors(cli, project) == [(f"{TASKS}/SVC-IMPL-007.yaml", "dependsOn[1].task", "SELF_DEPENDENCY")]


def test_missing_dependency_target(cli, project: Path) -> None:  # type: ignore[no-untyped-def]
    _edit(
        project / TASKS / "SVC-IMPL-007.yaml",
        lambda d: d["dependsOn"].append({"task": "NOPE-1", "gate": "start", "requires": "merged"}),
    )
    assert _errors(cli, project) == [(f"{TASKS}/SVC-IMPL-007.yaml", "dependsOn[1].task", "UNKNOWN_DEPENDENCY")]


def test_duplicate_dependency_edge(cli, project: Path) -> None:  # type: ignore[no-untyped-def]
    _edit(
        project / TASKS / "SVC-IMPL-007.yaml",
        lambda d: d["dependsOn"].append({"task": "SVC-CONTRACT-002", "gate": "start", "requires": "validated"}),
    )
    assert _errors(cli, project) == [(f"{TASKS}/SVC-IMPL-007.yaml", "dependsOn[1]", "DUPLICATE_DEPENDENCY")]


def test_invalid_yaml(cli, project: Path) -> None:  # type: ignore[no-untyped-def]
    (project / TASKS / "PLAN-SPIKE-001.yaml").write_text("id: [unclosed\n")
    code, _, err = cli("validate", project)
    assert code == 2
    assert f"{TASKS}/PLAN-SPIKE-001.yaml: [INVALID_YAML] invalid YAML (line" in err
    # The task id is unreadable, so the reference to it is reported too.
    assert _errors(cli, project) == [
        (f"{TASKS}/PLAN-SPIKE-001.yaml", "", "INVALID_YAML"),
        (f"{TASKS}/SVC-CONTRACT-002.yaml", "dependsOn[0].task", "UNKNOWN_DEPENDENCY"),
    ]


def test_duplicate_yaml_keys(cli, project: Path) -> None:  # type: ignore[no-untyped-def]
    path = project / TASKS / "PLAN-SPIKE-001.yaml"
    path.write_text(path.read_text() + "repository: svc\n")
    assert _errors(cli, project) == [
        (f"{TASKS}/PLAN-SPIKE-001.yaml", "", "INVALID_YAML"),
        (f"{TASKS}/SVC-CONTRACT-002.yaml", "dependsOn[0].task", "UNKNOWN_DEPENDENCY"),
    ]


def test_yml_extension_is_rejected_not_ignored(cli, project: Path) -> None:  # type: ignore[no-untyped-def]
    (project / TASKS / "EXTRA.yml").write_text("id: EXTRA\n")
    assert _errors(cli, project) == [(f"{TASKS}/EXTRA.yml", "", "UNSUPPORTED_EXTENSION")]


def test_unknown_profile(cli, project: Path) -> None:  # type: ignore[no-untyped-def]
    _edit(project / TASKS / "SVC-IMPL-007.yaml", lambda d: d["architecture"].update(profile="ghost-arch"))
    assert _errors(cli, project) == [(f"{TASKS}/SVC-IMPL-007.yaml", "architecture.profile", "UNKNOWN_PROFILE")]


def test_profile_for_other_repository(cli, project: Path) -> None:  # type: ignore[no-untyped-def]
    _edit(project / TASKS / "SVC-IMPL-007.yaml", lambda d: d["architecture"].update(profile="client-arch"))
    assert _errors(cli, project) == [
        (f"{TASKS}/SVC-IMPL-007.yaml", "architecture.profile", "PROFILE_REPOSITORY_MISMATCH")
    ]


def test_profile_file_repository_disagrees_with_manifest(cli, project: Path) -> None:  # type: ignore[no-untyped-def]
    _edit(project / "factory/profiles/client.yaml", lambda d: d.update(repository="svc", enforcedBy=[]))
    assert _errors(cli, project) == [("factory/profiles/client.yaml", "repository", "PROFILE_REPOSITORY_MISMATCH")]


def test_missing_profile_file(cli, project: Path) -> None:  # type: ignore[no-untyped-def]
    (project / "factory/profiles/client.yaml").unlink()
    assert _errors(cli, project) == [("factory/project.yaml", "architecture.profiles[1].path", "PROFILE_NOT_FOUND")]


def test_missing_adr_file(cli, project: Path) -> None:  # type: ignore[no-untyped-def]
    (project / "docs/adr/ADR-0002-module-layering.md").unlink()
    assert _errors(cli, project) == [
        ("factory/profiles/svc.yaml", "adrs[1].id", "ADR_NOT_FOUND"),
        ("factory/profiles/svc.yaml", "manualOnly[0]", "UNKNOWN_CONSTRAINT"),
    ]


@pytest.mark.parametrize(("value", "code"), [("0", "INVALID_VALUE"), ("two", "INVALID_TYPE"), ("1.5", "INVALID_TYPE")])
def test_invalid_adr_revision(cli, project: Path, value: str, code: str) -> None:  # type: ignore[no-untyped-def]
    adr = project / "docs/adr/ADR-0001-contract-source.md"
    adr.write_text(adr.read_text().replace("revision: 1", f"revision: {value}", 1))
    errors = _errors(cli, project)
    assert ("docs/adr/ADR-0001-contract-source.md", "revision", code) in errors


def test_adr_without_critique_is_rejected(cli, project: Path) -> None:  # type: ignore[no-untyped-def]
    adr = project / "docs/adr/ADR-0001-contract-source.md"
    adr.write_text(adr.read_text().replace("critique: [docs/adr/research/ADR-0001-critique.md]\n", ""))
    assert ("docs/adr/ADR-0001-contract-source.md", "critique", "MISSING_FIELD") in _errors(cli, project)


def test_adr_research_link_must_resolve(cli, project: Path) -> None:  # type: ignore[no-untyped-def]
    (project / "docs/adr/research/ADR-0001-options.md").unlink()
    assert _errors(cli, project) == [
        ("docs/adr/ADR-0001-contract-source.md", "research[0]", "REFERENCED_FILE_NOT_FOUND")
    ]


def test_adr_with_unterminated_front_matter(cli, project: Path) -> None:  # type: ignore[no-untyped-def]
    (project / "docs/adr/ADR-0009.md").write_text("---\nid: ADR-0009\n")
    assert _errors(cli, project) == [("docs/adr/ADR-0009.md", "", "INVALID_FRONT_MATTER")]


def test_duplicate_adr_ids(cli, project: Path) -> None:  # type: ignore[no-untyped-def]
    source = project / "docs/adr/ADR-0001-contract-source.md"
    (project / "docs/adr/ADR-0001-copy.md").write_text(source.read_text())
    assert _errors(cli, project) == [("docs/adr/ADR-0001-copy.md", "id", "DUPLICATE_ADR_ID")]


def test_missing_specification_file(cli, project: Path) -> None:  # type: ignore[no-untyped-def]
    (project / "specs/stock-help.md").unlink()
    assert _errors(cli, project) == [
        (f"{TASKS}/CLIENT-DOCS-005.yaml", "specification.path", "REFERENCED_FILE_NOT_FOUND")
    ]


def test_specification_outside_project_is_rejected(cli, project: Path) -> None:  # type: ignore[no-untyped-def]
    _edit(project / TASKS / "CLIENT-DOCS-005.yaml", lambda d: d["specification"].update(path="../../etc/hosts"))
    assert _errors(cli, project) == [(f"{TASKS}/CLIENT-DOCS-005.yaml", "specification.path", "INVALID_FORMAT")]


def test_unknown_output_and_invalid_range(cli, project: Path) -> None:  # type: ignore[no-untyped-def]
    _edit(
        project / TASKS / "CLIENT-UI-003.yaml", lambda d: d["consumes"].append({"output": "ghost", "version": "latest"})
    )
    errors = _errors(cli, project)
    assert (f"{TASKS}/CLIENT-UI-003.yaml", "consumes[1].output", "UNKNOWN_OUTPUT") in errors
    assert (f"{TASKS}/CLIENT-UI-003.yaml", "consumes[1].version", "INVALID_VERSION_RANGE") in errors


def test_task_cannot_provide_another_repositorys_output(cli, project: Path) -> None:  # type: ignore[no-untyped-def]
    _edit(project / TASKS / "CLIENT-DOCS-005.yaml", lambda d: d.update(provides=["svc-contract"]))
    assert _errors(cli, project) == [(f"{TASKS}/CLIENT-DOCS-005.yaml", "provides[0]", "OUTPUT_REPOSITORY_MISMATCH")]


def test_manifest_reference_errors(cli, project: Path) -> None:  # type: ignore[no-untyped-def]
    def change(d: dict[str, Any]) -> None:
        d["planningRepository"] = "nowhere"
        d["repositories"][1]["github"] = d["repositories"][0]["github"].upper()
        d["repositories"][2]["branches"]["release"] = "dev"
        d["architecture"]["profiles"][0]["repository"] = "ghost"

    _edit(project / "factory/project.yaml", change)
    errors = {(f, c) for f, _, c in _errors(cli, project)}
    assert {
        ("factory/project.yaml", "UNKNOWN_REPOSITORY"),
        ("factory/project.yaml", "DUPLICATE_GITHUB_REPOSITORY"),
        ("factory/project.yaml", "AMBIGUOUS_BRANCHES"),
        ("factory/profiles/svc.yaml", "PROFILE_REPOSITORY_MISMATCH"),
    } <= errors


def test_dispatch_cannot_be_enabled(cli, project: Path) -> None:  # type: ignore[no-untyped-def]
    _edit(project / "factory/project.yaml", lambda d: d.update(dispatch={"enabled": True}))
    assert _errors(cli, project) == [("factory/project.yaml", "dispatch.enabled", "DISPATCH_NOT_SUPPORTED")]


def test_dispatch_defaults_to_disabled(cli) -> None:  # type: ignore[no-untyped-def]
    # The single-repo fixture declares `dispatch: {}`.
    code, _, _ = cli("validate", SINGLE_REPO)
    assert code == 0


def test_missing_project_and_manifest(cli, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    assert _errors(cli, tmp_path / "nope") == [("", "", "PROJECT_NOT_FOUND")]
    assert _errors(cli, tmp_path) == [("factory/project.yaml", "", "MANIFEST_NOT_FOUND")]


def test_usage_errors_exit_64(cli) -> None:  # type: ignore[no-untyped-def]
    assert cli()[0] == 64
    assert cli("plan", THREE_REPO)[0] == 64  # --evidence is required
    assert cli("validate", THREE_REPO, "--format", "xml")[0] == 64


# -- malformed evidence ------------------------------------------------------------


def _plan_errors(cli, tmp_path: Path, content: str) -> list[tuple[str, str]]:  # type: ignore[no-untyped-def]
    path = tmp_path / "snapshot.json"
    path.write_text(content)
    code, out, _ = cli("plan", THREE_REPO, "--evidence", path, "--format", "json")
    assert code == 2, out
    return [(e["field"], e["code"]) for e in json.loads(out)["errors"]]


def _snapshot_text(change: Callable[[dict[str, Any]], None]) -> str:
    document = three_repo_baseline()
    change(document)
    return json.dumps(document)


def test_evidence_not_json(cli, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    assert _plan_errors(cli, tmp_path, "{not json") == [("", "INVALID_JSON")]


def test_evidence_duplicate_keys(cli, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    text = json.dumps(three_repo_baseline())
    text = text.replace('"project": "example-three"', '"project": "example-three", "project": "other"')
    assert _plan_errors(cli, tmp_path, text) == [("", "INVALID_JSON")]


def test_evidence_schema_violations(cli, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    def change(d: dict[str, Any]) -> None:
        d["provenance"]["kind"] = "github"
        d["approvals"][0]["sha256"] = "short"
        d["repositories"]["svc"]["integrationHead"] = "HEAD"
        d["unexpected"] = 1

    assert set(_plan_errors(cli, tmp_path, _snapshot_text(change))) == {
        ("provenance.kind", "INVALID_VALUE"),
        ("approvals[0].sha256", "INVALID_FORMAT"),
        ("repositories.svc.integrationHead", "INVALID_FORMAT"),
        ("unexpected", "UNKNOWN_FIELD"),
    }


def test_evidence_semantic_errors(cli, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    def change(d: dict[str, Any]) -> None:
        d["project"] = "someone-else"
        d["repositories"]["ghost"] = {}
        d["approvals"].append(dict(d["approvals"][0]))

    assert _plan_errors(cli, tmp_path, _snapshot_text(change)) == [
        ("approvals", "AMBIGUOUS_APPROVAL"),
        ("project", "SNAPSHOT_PROJECT_MISMATCH"),
        ("repositories.ghost", "UNKNOWN_REPOSITORY"),
    ]


def test_evidence_file_missing(cli, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    code, out, _ = cli("plan", THREE_REPO, "--evidence", tmp_path / "none.json", "--format", "json")
    assert code == 2
    assert json.loads(out)["errors"][0]["code"] == "EVIDENCE_NOT_FOUND"


def test_plan_refuses_invalid_project(cli, project: Path, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    snapshot = write_snapshot(three_repo_baseline(), tmp_path / "s.json")
    _edit(project / TASKS / "SVC-IMPL-007.yaml", lambda d: d.update(repository="web"))
    code, out, err = cli("plan", project, "--evidence", snapshot)
    assert code == 2
    assert out == ""
    assert "UNKNOWN_REPOSITORY" in err


def test_text_plan_states_evidence_is_not_authenticated(cli, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    snapshot = write_snapshot(three_repo_baseline(), tmp_path / "s.json")
    code, out, err = cli("plan", THREE_REPO, "--evidence", snapshot)
    assert code == 0 and err == ""
    assert "NOT authenticated" in out
    assert "CLIENT-INT-004 (revision 1, BLOCKED, branch feat/CLIENT-INT-004-r1)" in out
    assert "NOT_MERGED (FAIL) [feat/CLIENT-UI-003-r1]" in out


# -- read-only guarantee ---------------------------------------------------------


def _tree_fingerprint(root: Path) -> dict[str, tuple[str, int]]:
    return {
        p.relative_to(root).as_posix(): (
            hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else "dir",
            p.stat().st_mtime_ns,
        )
        for p in sorted(root.rglob("*"))
    }


def test_commands_never_write_to_the_project(project: Path, tmp_path: Path) -> None:
    snapshot = write_snapshot(three_repo_baseline(project), tmp_path / "s.json")
    before = _tree_fingerprint(project)
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "0"}
    for args in (["validate", str(project)], ["plan", str(project), "--evidence", str(snapshot), "--format", "json"]):
        subprocess.run([sys.executable, "-m", "factory", *args], check=True, capture_output=True, cwd=project, env=env)
    assert _tree_fingerprint(project) == before


def test_entry_point_and_version() -> None:
    result = subprocess.run([sys.executable, "-m", "factory", "--version"], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "factory 0.1.0"


def test_symlinked_task_outside_project_is_rejected(cli, project: Path, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    outside = tmp_path / "outside.yaml"
    outside.write_text((project / TASKS / "PLAN-SPIKE-001.yaml").read_text())
    (project / TASKS / "LINKED.yaml").symlink_to(outside)
    assert _errors(cli, project) == [(f"{TASKS}/LINKED.yaml", "", "PATH_OUTSIDE_PROJECT")]
