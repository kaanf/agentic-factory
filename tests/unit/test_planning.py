"""Approval, architecture gate, gate composition and state precedence."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from conftest import codes, task_of
from support import C1, I1, THREE_REPO, copy_project, fake_sha, merged_pr, run, three_repo_baseline


def _approval(snapshot: dict[str, Any], path: str) -> dict[str, Any]:
    return next(a for a in snapshot["approvals"] if a["path"] == path)


def _blank_slate(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Nothing merged anywhere; every branch known to have no PR."""
    for repo in snapshot["repositories"].values():
        for entry in repo["branches"].values():
            entry["pullRequests"] = []
    return snapshot


def test_missing_approval_is_unknown(plan_json) -> None:  # type: ignore[no-untyped-def]
    snapshot = three_repo_baseline()
    snapshot["approvals"].remove(_approval(snapshot, "factory/tasks/PLAN-SPIKE-001.yaml"))
    task = task_of(plan_json(THREE_REPO, snapshot), "PLAN-SPIKE-001")
    assert task["gates"]["start"]["result"] == "UNKNOWN"
    assert task["state"] == "UNKNOWN"
    assert {"TASK_NOT_APPROVED", "APPROVAL_EVIDENCE_MISSING"} <= codes(task["gates"]["start"]["reasons"])


def test_rejected_and_unauthorized_approvals_fail(plan_json) -> None:  # type: ignore[no-untyped-def]
    snapshot = three_repo_baseline()
    _approval(snapshot, "factory/tasks/PLAN-SPIKE-001.yaml")["approved"] = False
    _approval(snapshot, "factory/tasks/CLIENT-DOCS-005.yaml")["mergedBy"] = "agent-bot"
    plan = plan_json(THREE_REPO, snapshot)
    assert task_of(plan, "PLAN-SPIKE-001")["state"] == "BLOCKED"
    assert "NOT_APPROVED" in codes(task_of(plan, "PLAN-SPIKE-001")["gates"]["start"]["reasons"])
    docs = task_of(plan, "CLIENT-DOCS-005")
    assert docs["state"] == "BLOCKED"
    assert "APPROVER_NOT_AUTHORIZED" in codes(docs["gates"]["start"]["reasons"])


def test_unapproved_profile_blocks_implementation(plan_json) -> None:  # type: ignore[no-untyped-def]
    snapshot = three_repo_baseline()
    _approval(snapshot, "factory/profiles/client.yaml")["approved"] = False
    plan = plan_json(THREE_REPO, snapshot)
    docs = task_of(plan, "CLIENT-DOCS-005")
    assert docs["state"] == "BLOCKED"
    assert docs["architecture"]["result"] == "FAIL"
    assert {"PROFILE_NOT_APPROVED", "NOT_APPROVED"} <= codes(docs["gates"]["start"]["reasons"])
    assert docs["gates"]["openPullRequest"]["result"] == "FAIL"
    # Spike tasks need no profile and are unaffected.
    spike = task_of(plan, "PLAN-SPIKE-001")
    assert spike["architecture"] == {
        "profile": None,
        "profileRevision": None,
        "reasons": [],
        "required": False,
        "result": "PASS",
    }


def test_profile_changed_after_approval_is_stale(plan_json, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    project = copy_project(THREE_REPO, tmp_path)
    snapshot = three_repo_baseline(project)
    profile = project / "factory/profiles/client.yaml"
    profile.write_text(profile.read_text() + "manualOnly: []\n")
    docs = task_of(plan_json(project, snapshot), "CLIENT-DOCS-005")
    assert docs["state"] == "STALE"
    assert codes(docs["stale"]) == {"PROFILE_CHANGED_SINCE_APPROVAL"}


def test_task_pinning_old_profile_revision_is_stale(plan_json, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    project = copy_project(THREE_REPO, tmp_path)
    profile = project / "factory/profiles/client.yaml"
    profile.write_text(profile.read_text().replace("revision: 1", "revision: 2", 1))
    plan = plan_json(project, three_repo_baseline(project))
    for task_id in ("CLIENT-UI-003", "CLIENT-INT-004", "CLIENT-DOCS-005"):
        assert codes(task_of(plan, task_id)["stale"]) == {"PROFILE_REVISION_MISMATCH"}


def test_unaccepted_or_unapproved_adr_blocks(plan_json, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    project = copy_project(THREE_REPO, tmp_path)
    adr = project / "docs/adr/ADR-0001-contract-source.md"
    adr.write_text(adr.read_text().replace("status: accepted", "status: proposed", 1))
    snapshot = three_repo_baseline(project)
    _approval(snapshot, "docs/adr/ADR-0002-module-layering.md")["approved"] = False
    plan = plan_json(project, snapshot)
    docs = task_of(plan, "CLIENT-DOCS-005")
    assert docs["state"] == "BLOCKED"
    assert codes(docs["architecture"]["reasons"]) == {"ADR_NOT_ACCEPTED"}
    impl = task_of(plan, "SVC-IMPL-007")
    assert {"ADR_NOT_ACCEPTED", "ADR_NOT_APPROVED"} <= codes(impl["architecture"]["reasons"])


def test_empty_profile_is_an_explicit_valid_choice(cli, plan_json, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    project = copy_project(THREE_REPO, tmp_path)
    (project / "factory/profiles/client.yaml").write_text(
        "schemaVersion: factory.profile/v1\nid: client-arch\nrevision: 1\nrepository: client\nadrs: []\n"
    )
    code, _, err = cli("validate", project)
    assert code == 0, err
    docs = task_of(plan_json(project, three_repo_baseline(project)), "CLIENT-DOCS-005")
    assert docs["architecture"]["result"] == "PASS"


def test_cross_repository_chain_unblocks_step_by_step(plan_json) -> None:  # type: ignore[no-untyped-def]
    snapshot = _blank_slate(three_repo_baseline())
    plan = plan_json(THREE_REPO, snapshot)
    states = {t["id"]: t["state"] for t in plan["tasks"]}
    # plan -> svc: contract may start once the spike is approved; nothing downstream may.
    assert states["PLAN-SPIKE-001"] == "READY"
    assert states["SVC-CONTRACT-002"] == "READY"
    assert states["SVC-IMPL-007"] == "BLOCKED"
    assert states["CLIENT-UI-003"] == "BLOCKED"
    assert states["CLIENT-INT-004"] == "BLOCKED"

    # svc -> client: merging and validating the contract unblocks the client UI and the svc implementation.
    snapshot["repositories"]["svc"]["branches"]["feat/SVC-CONTRACT-002-r1"]["pullRequests"] = [merged_pr(11, C1)]
    snapshot["repositories"]["svc"]["integrationHead"] = C1
    plan = plan_json(THREE_REPO, snapshot)
    states = {t["id"]: t["state"] for t in plan["tasks"]}
    assert states["SVC-CONTRACT-002"] == "COMPLETE"
    assert states["SVC-IMPL-007"] == "READY"
    assert states["CLIENT-UI-003"] == "READY"
    assert states["CLIENT-INT-004"] == "BLOCKED"  # still needs CLIENT-UI-003 merged
    blockers = {r["ref"] for r in task_of(plan, "CLIENT-INT-004")["gates"]["start"]["reasons"]}
    assert blockers == {"CLIENT-UI-003"}


def test_mixed_gates_are_evaluated_separately(plan_json) -> None:  # type: ignore[no-untyped-def]
    snapshot = three_repo_baseline()
    snapshot["repositories"]["client"]["branches"]["feat/CLIENT-UI-003-r1"]["pullRequests"] = [
        merged_pr(21, fake_sha("ui merge"))
    ]
    plan = plan_json(THREE_REPO, snapshot)
    edges = {(d["task"], d["gate"]): d for d in task_of(plan, "CLIENT-INT-004")["dependencies"]}
    assert edges[("SVC-CONTRACT-002", "start")]["result"] == "PASS"
    assert edges[("CLIENT-UI-003", "start")]["result"] == "PASS"
    assert edges[("SVC-IMPL-007", "merge")]["result"] == "PASS"
    assert edges[("SVC-IMPL-007", "complete")]["result"] == "UNKNOWN"
    assert edges[("SVC-IMPL-007", "complete")]["dependencyLevel"] == "integration-ready"


def test_open_pr_gate_ignores_dependencies_but_not_own_preconditions(plan_json) -> None:  # type: ignore[no-untyped-def]
    plan = plan_json(THREE_REPO, three_repo_baseline())
    integration = task_of(plan, "CLIENT-INT-004")
    assert integration["gates"]["start"]["result"] == "FAIL"
    assert integration["gates"]["openPullRequest"]["result"] == "PASS"


def test_blocked_takes_precedence_over_in_review(plan_json) -> None:  # type: ignore[no-untyped-def]
    snapshot = three_repo_baseline()
    snapshot["repositories"]["client"]["branches"]["feat/CLIENT-INT-004-r1"]["pullRequests"] = [
        {"baseBranch": "dev", "mergeCommitSha": None, "number": 30, "state": "open"}
    ]
    plan = plan_json(THREE_REPO, snapshot)
    assert task_of(plan, "CLIENT-INT-004")["state"] == "BLOCKED"
    assert task_of(plan, "CLIENT-UI-003")["state"] == "IN_REVIEW"


def test_merged_task_with_failing_ci_is_merged_not_complete(plan_json) -> None:  # type: ignore[no-untyped-def]
    snapshot = three_repo_baseline()
    snapshot["repositories"]["svc"]["commits"][I1]["checkRuns"] = [run("Svc Check", "dev", conclusion="failure")]
    impl = task_of(plan_json(THREE_REPO, snapshot), "SVC-IMPL-007")
    assert impl["state"] == "MERGED"
    assert impl["gates"]["complete"]["result"] == "FAIL"


def test_plan_output_records_simulated_provenance(plan_json) -> None:  # type: ignore[no-untyped-def]
    plan = plan_json(THREE_REPO, three_repo_baseline())
    assert plan["evidence"] == {"authenticated": False, "observedAt": "2026-11-11T12:00:00Z", "provenance": "simulated"}
    assert plan["schemaVersion"] == "factory.plan/v1"
    assert plan["summary"]["COMPLETE"] == 2
    assert "factory/tasks/SVC-IMPL-007.yaml" in plan["artifacts"]


def test_spike_dependency_with_approved_level(plan_json) -> None:  # type: ignore[no-untyped-def]
    snapshot = three_repo_baseline()
    snapshot["approvals"].remove(_approval(snapshot, "factory/tasks/PLAN-SPIKE-001.yaml"))
    contract_task = task_of(plan_json(THREE_REPO, _blank_slate(snapshot)), "SVC-CONTRACT-002")
    assert contract_task["state"] == "UNKNOWN"
    edge = contract_task["dependencies"][0]
    assert (edge["task"], edge["requires"], edge["result"]) == ("PLAN-SPIKE-001", "approved", "UNKNOWN")
