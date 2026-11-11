"""Stage 1 acceptance suite (Stage 4B §11, T1-T16). One test per acceptance criterion."""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from conftest import NetworkAccessError, codes, task_of
from support import (
    I1,
    SINGLE_REPO,
    THREE_REPO,
    commit,
    contract,
    copy_project,
    fake_sha,
    run,
    single_repo_baseline,
    three_repo_baseline,
    three_repo_merged_without_push_ci,
    write_snapshot,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _edit_yaml(path: Path, change) -> None:  # type: ignore[no-untyped-def]
    document = yaml.safe_load(path.read_text())
    change(document)
    path.write_text(yaml.safe_dump(document, sort_keys=False))


def test_T01_valid_three_repository_fixture_validates(cli) -> None:  # type: ignore[no-untyped-def]
    code, out, err = cli("validate", THREE_REPO)
    assert code == 0
    assert err == ""
    assert "3 repositories" in out

    code, out, _ = cli("validate", THREE_REPO, "--format", "json")
    report = json.loads(out)
    assert report["valid"] is True
    assert report["errors"] == [] and report["warnings"] == []


def test_T02_cycle_is_rejected_and_reported(cli, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    project = copy_project(THREE_REPO, tmp_path)
    # CLIENT-UI-003 -> SVC-CONTRACT-002 already exists; close the loop with mixed gates.
    _edit_yaml(
        project / "factory/tasks/SVC-CONTRACT-002.yaml",
        lambda d: d["dependsOn"].append({"task": "CLIENT-INT-004", "gate": "complete", "requires": "merged"}),
    )
    code, out, err = cli("validate", project, "--format", "json")
    assert code == 2
    cycles = [e for e in json.loads(out)["errors"] if e["code"] == "DEPENDENCY_CYCLE"]
    assert len(cycles) == 1
    message = cycles[0]["message"]
    assert "CLIENT-INT-004, CLIENT-UI-003, SVC-CONTRACT-002" in message
    assert "CLIENT-INT-004 -> SVC-CONTRACT-002 -> CLIENT-INT-004" in message


def test_T03_unknown_repository_cites_file_and_field(cli, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    project = copy_project(THREE_REPO, tmp_path)
    _edit_yaml(project / "factory/tasks/PLAN-SPIKE-001.yaml", lambda d: d.update(repository="web"))
    code, out, err = cli("validate", project)
    assert code == 2
    assert "factory/tasks/PLAN-SPIKE-001.yaml: repository: [UNKNOWN_REPOSITORY]" in err
    assert "'web'" in err


@pytest.mark.parametrize("key", ["approvedBy", "approval", "approved_by", "approvedAt", "signedOffBy"])
def test_T04_self_declared_approval_is_rejected(cli, tmp_path: Path, key: str) -> None:  # type: ignore[no-untyped-def]
    project = copy_project(THREE_REPO, tmp_path)
    _edit_yaml(project / "factory/tasks/SVC-IMPL-007.yaml", lambda d: d.update({key: "example-human"}))
    code, out, _ = cli("validate", project, "--format", "json")
    assert code == 2
    errors = json.loads(out)["errors"]
    assert [(e["code"], e["field"]) for e in errors] == [("SELF_DECLARED_APPROVAL", key)]
    assert "derived from verified evidence" in errors[0]["message"]


def test_T04_nested_approval_in_profile_and_adr_is_rejected(cli, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    project = copy_project(THREE_REPO, tmp_path)
    _edit_yaml(project / "factory/profiles/svc.yaml", lambda d: d["adrs"][0].update(approvedBy="x"))
    adr = project / "docs/adr/ADR-0002-module-layering.md"
    adr.write_text(adr.read_text().replace("revision: 1\n", "revision: 1\napprovedBy: example-human\n", 1))
    code, out, _ = cli("validate", project, "--format", "json")
    found = {(e["file"], e["field"]) for e in json.loads(out)["errors"] if e["code"] == "SELF_DECLARED_APPROVAL"}
    assert found == {
        ("factory/profiles/svc.yaml", "adrs[0].approvedBy"),
        ("docs/adr/ADR-0002-module-layering.md", "approvedBy"),
    }


def test_T05_unknown_assurance_level_is_rejected(cli, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    project = copy_project(THREE_REPO, tmp_path)
    _edit_yaml(project / "factory/tasks/SVC-IMPL-007.yaml", lambda d: d["dependsOn"][0].update(requires="deployed"))
    code, out, _ = cli("validate", project, "--format", "json")
    assert code == 2
    errors = json.loads(out)["errors"]
    assert [(e["code"], e["field"]) for e in errors] == [("INVALID_VALUE", "dependsOn[0].requires")]
    for level in ("approved", "merged", "validated", "integration-ready", "environment-verified"):
        assert level in errors[0]["message"]


def test_T06_implementation_task_without_profile_is_rejected(cli, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    project = copy_project(THREE_REPO, tmp_path)
    _edit_yaml(project / "factory/tasks/SVC-IMPL-007.yaml", lambda d: d.pop("architecture"))
    code, out, _ = cli("validate", project, "--format", "json")
    assert code == 2
    errors = json.loads(out)["errors"]
    assert [(e["file"], e["code"], e["field"]) for e in errors] == [
        ("factory/tasks/SVC-IMPL-007.yaml", "MISSING_FIELD", "architecture")
    ]


def test_T07_merged_without_integration_ci_does_not_satisfy_validated(plan_json) -> None:  # type: ignore[no-untyped-def]
    plan = plan_json(THREE_REPO, three_repo_merged_without_push_ci())
    dependency = task_of(plan, "SVC-IMPL-007")
    assert dependency["achievedLevel"] == "merged"
    assert dependency["levels"]["validated"]["result"] == "UNKNOWN"

    dependent = task_of(plan, "CLIENT-DOCS-005")  # gate: start, requires: validated
    start = dependent["gates"]["start"]
    assert start["result"] != "PASS"
    assert {"DEP_LEVEL_UNMET", "EVIDENCE_UNKNOWN", "CHECK_MISSING"} <= codes(start["reasons"])
    assert dependent["state"] in {"BLOCKED", "UNKNOWN"}


def test_T08_validated_task_with_unvalidated_head_is_not_integration_ready(plan_json) -> None:  # type: ignore[no-untyped-def]
    snapshot = three_repo_baseline()
    svc = snapshot["repositories"]["svc"]
    head = fake_sha("later unrelated merge")
    svc["integrationHead"] = head
    svc["commits"][head] = commit(13, [run("Svc Check", "dev", conclusion=None, status="in_progress")], contract())
    plan = plan_json(THREE_REPO, snapshot)

    impl = task_of(plan, "SVC-IMPL-007")
    assert impl["levels"]["validated"]["result"] == "PASS"
    assert impl["levels"]["integration-ready"]["result"] == "UNKNOWN"
    assert impl["achievedLevel"] == "validated"

    integration = task_of(plan, "CLIENT-INT-004")
    deps = {(d["task"], d["gate"]): d for d in integration["dependencies"]}
    assert deps[("SVC-IMPL-007", "merge")]["result"] != "PASS"
    assert integration["gates"]["merge"]["result"] != "PASS"
    assert "HEAD_NOT_VALIDATED" in codes(integration["gates"]["merge"]["reasons"])
    assert integration["gates"]["complete"]["result"] != "PASS"


def test_T09_validated_head_with_compatible_contract_passes_merge_gate(plan_json) -> None:  # type: ignore[no-untyped-def]
    snapshot = three_repo_baseline()
    snapshot["repositories"]["client"]["branches"]["feat/CLIENT-UI-003-r1"]["pullRequests"] = [
        {"baseBranch": "dev", "mergeCommitSha": fake_sha("ui merge"), "number": 21, "state": "merged"}
    ]
    plan = plan_json(THREE_REPO, snapshot)
    integration = task_of(plan, "CLIENT-INT-004")
    assert integration["gates"]["start"]["result"] == "PASS"
    assert integration["gates"]["merge"]["result"] == "PASS"
    complete = integration["gates"]["complete"]
    assert complete["result"] != "PASS"
    assert "ENVIRONMENT_EVIDENCE_UNSUPPORTED" in codes(complete["reasons"])

    # The same evidence with an incompatible contract version fails the merge gate.
    snapshot["repositories"]["svc"]["commits"][I1]["outputs"] = contract("2.0.0")
    plan = plan_json(THREE_REPO, snapshot)
    merge = task_of(plan, "CLIENT-INT-004")["gates"]["merge"]
    assert merge["result"] == "FAIL"
    assert "CONTRACT_INCOMPATIBLE" in codes(merge["reasons"])


def test_T10_changed_specification_makes_task_stale(plan_json, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    project = copy_project(THREE_REPO, tmp_path)
    snapshot = three_repo_baseline(project)
    (project / "specs/stock-help.md").write_text("# Help text, rewritten after approval\n")
    plan = plan_json(project, snapshot)
    task = task_of(plan, "CLIENT-DOCS-005")
    assert task["state"] == "STALE"
    assert codes(task["stale"]) == {"SPEC_CHANGED"}
    assert task["gates"]["start"]["result"] == "FAIL"


def test_T10_task_file_changed_after_approval_is_stale(plan_json, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    project = copy_project(THREE_REPO, tmp_path)
    snapshot = three_repo_baseline(project)
    _edit_yaml(project / "factory/tasks/CLIENT-DOCS-005.yaml", lambda d: d["acceptance"].append("New criterion"))
    task = task_of(plan_json(project, snapshot), "CLIENT-DOCS-005")
    assert task["state"] == "STALE"
    assert codes(task["stale"]) == {"TASK_CHANGED_SINCE_APPROVAL"}


def test_T11_missing_evidence_is_unknown_never_pass(plan_json) -> None:  # type: ignore[no-untyped-def]
    snapshot = three_repo_baseline()
    del snapshot["repositories"]["svc"]["commits"][I1]["checkRuns"]
    plan = plan_json(THREE_REPO, snapshot)
    impl = task_of(plan, "SVC-IMPL-007")
    assert impl["levels"]["validated"]["result"] == "UNKNOWN"
    assert "CHECK_EVIDENCE_MISSING" in codes(impl["levels"]["validated"]["reasons"])
    docs = task_of(plan, "CLIENT-DOCS-005")
    assert docs["gates"]["start"]["result"] == "UNKNOWN"
    assert docs["state"] == "UNKNOWN"

    # An empty snapshot proves nothing: no task may start.
    empty = three_repo_baseline()
    empty["approvals"] = []
    empty["repositories"] = {}
    plan = plan_json(THREE_REPO, empty)
    for task in plan["tasks"]:
        assert task["gates"]["start"]["result"] == "UNKNOWN", task["id"]
        assert task["state"] == "UNKNOWN"
        assert "APPROVAL_EVIDENCE_MISSING" in codes(task["gates"]["start"]["reasons"])


def test_T12_outdated_adr_revision_makes_dependent_tasks_stale(cli, plan_json, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    project = copy_project(THREE_REPO, tmp_path)
    adr = project / "docs/adr/ADR-0002-module-layering.md"
    adr.write_text(adr.read_text().replace("revision: 1", "revision: 2", 1))
    snapshot = three_repo_baseline(project)  # ADR approved at revision 2; svc profile still pins revision 1

    code, out, _ = cli("validate", project, "--format", "json")
    assert code == 0
    assert [w["code"] for w in json.loads(out)["warnings"]] == ["ADR_REVISION_MISMATCH"]

    plan = plan_json(project, snapshot)
    for task_id in ("SVC-CONTRACT-002", "SVC-IMPL-007"):
        task = task_of(plan, task_id)
        assert task["state"] == "STALE"
        assert codes(task["stale"]) == {"ADR_REVISION_MISMATCH"}
    # client-arch does not pin ADR-0002, so client tasks are not stale.
    assert task_of(plan, "CLIENT-DOCS-005")["state"] != "STALE"


def test_T13_identical_inputs_give_byte_identical_json(tmp_path: Path) -> None:
    snapshot = write_snapshot(three_repo_merged_without_push_ci(), tmp_path / "s.json")
    outputs = []
    for seed in ("0", "1", "12345"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        result = subprocess.run(
            [sys.executable, "-m", "factory", "plan", str(THREE_REPO), "--evidence", str(snapshot), "--format", "json"],
            capture_output=True,
            env=env,
            check=True,
        )
        outputs.append(result.stdout)
    assert outputs[0] == outputs[1] == outputs[2]
    assert b"observedAt" in outputs[0]
    # No generated timestamps: the only time value is the one copied from the snapshot.
    assert re.findall(rb"\d{4}-\d{2}-\d{2}T", outputs[0]) == [b"2026-11-11T"]


def test_T14_single_repo_non_kotlin_project_with_other_branch_names(cli, plan_json) -> None:  # type: ignore[no-untyped-def]
    code, _, err = cli("validate", SINGLE_REPO)
    assert code == 0 and err == ""
    plan = plan_json(SINGLE_REPO, single_repo_baseline())
    states = {t["id"]: t["state"] for t in plan["tasks"]}
    assert states == {"TOOL-1": "COMPLETE", "TOOL-2": "COMPLETE", "TOOL-3": "READY"}
    assert task_of(plan, "TOOL-3")["branch"] == "topic/TOOL-3-r1"

    # Checks on a branch named like another project's integration branch do not count here.
    snapshot = single_repo_baseline()
    for facts in snapshot["repositories"]["tool"]["commits"].values():
        for check_run in facts["checkRuns"]:
            check_run["headBranch"] = "dev"
    plan = plan_json(SINGLE_REPO, snapshot)
    assert task_of(plan, "TOOL-1")["levels"]["validated"]["result"] == "UNKNOWN"


DENYLIST = re.compile(
    r"\b(bar|inventory|kaanf|kotlin|spring|kmp|crew|dev|main|master|trunk|develop)\b|bar-inventory",
    re.IGNORECASE,
)


def test_T15_production_source_has_no_project_identifiers() -> None:
    scanned = sorted((REPO_ROOT / "src").rglob("*.py")) + sorted((REPO_ROOT / "src").rglob("*.json"))
    assert len(scanned) >= 10
    hits = [
        f"{path.relative_to(REPO_ROOT)}:{number}: {line.strip()}"
        for path in scanned
        for number, line in enumerate(path.read_text().splitlines(), 1)
        if DENYLIST.search(line)
    ]
    assert hits == []


def test_T16_offline_guard_blocks_network() -> None:
    with pytest.raises(NetworkAccessError):
        socket.create_connection(("192.0.2.1", 443), timeout=1)
    with pytest.raises(NetworkAccessError):
        socket.getaddrinfo("example.com", 443)


def test_T16_cli_runs_with_sockets_disabled_in_a_fresh_process(tmp_path: Path) -> None:
    snapshot = write_snapshot(three_repo_baseline(), tmp_path / "s.json")
    guard = (
        "import socket, sys\n"
        "def blocked(*a, **k):\n"
        "    raise SystemExit('network access attempted')\n"
        "socket.socket.connect = blocked\n"
        "socket.socket.connect_ex = blocked\n"
        "socket.create_connection = blocked\n"
        "socket.getaddrinfo = blocked\n"
        "from factory.cli import run\n"
        "sys.exit(run(sys.argv[1:]))\n"
    )
    for args in (["validate", str(THREE_REPO)], ["plan", str(THREE_REPO), "--evidence", str(snapshot)]):
        result = subprocess.run([sys.executable, "-c", guard, *args], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
