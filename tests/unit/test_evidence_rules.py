"""Evidence rules for merged / validated / integration-ready levels, driven by snapshot fixtures."""

from __future__ import annotations

from typing import Any

import pytest
from conftest import codes, task_of
from support import C1, I1, THREE_REPO, contract, fake_sha, merged_pr, open_pr, run, three_repo_baseline


def _impl_levels(plan_json, snapshot: dict[str, Any]) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    return task_of(plan_json(THREE_REPO, snapshot), "SVC-IMPL-007")["levels"]


def _with_runs(*runs: dict[str, Any]) -> dict[str, Any]:
    snapshot = three_repo_baseline()
    snapshot["repositories"]["svc"]["commits"][I1]["checkRuns"] = list(runs)
    return snapshot


@pytest.mark.parametrize(
    ("runs", "result", "code"),
    [
        ([run("Svc Check", "dev")], "PASS", None),
        ([run("Svc Check", "feat/x", event="pull_request")], "UNKNOWN", "CHECK_MISSING"),
        ([], "UNKNOWN", "CHECK_MISSING"),
        ([run("Other Check", "dev")], "UNKNOWN", "CHECK_MISSING"),
        ([run("Svc Check", "release-branch")], "UNKNOWN", "CHECK_MISSING"),
        ([run("Svc Check", "dev", app="some-bot")], "FAIL", "CHECK_PROVENANCE_MISMATCH"),
        ([run("Svc Check", "dev", workflow=".github/workflows/edited.yml")], "FAIL", "CHECK_PROVENANCE_MISMATCH"),
        ([run("Svc Check", "dev", conclusion="failure")], "FAIL", "CHECK_FAILED"),
        ([run("Svc Check", "dev", conclusion="cancelled")], "FAIL", "CHECK_FAILED"),
        ([run("Svc Check", "dev", conclusion=None, status="in_progress")], "UNKNOWN", "CHECK_IN_PROGRESS"),
        ([run("Svc Check", "dev", conclusion="neutral")], "UNKNOWN", "CHECK_INCONCLUSIVE"),
        ([run("Svc Check", "dev", conclusion="skipped")], "UNKNOWN", "CHECK_INCONCLUSIVE"),
        ([run("Svc Check", "dev", conclusion="stale")], "UNKNOWN", "CHECK_INCONCLUSIVE"),
        (
            [run("Svc Check", "dev", conclusion="failure"), run("Svc Check", "dev", attempt=2)],
            "PASS",
            None,
        ),
        (
            [run("Svc Check", "dev"), run("Svc Check", "dev", conclusion="failure", attempt=2)],
            "FAIL",
            "CHECK_FAILED",
        ),
        (
            [run("Svc Check", "dev"), run("Svc Check", "dev", conclusion="failure")],
            "UNKNOWN",
            "AMBIGUOUS_CHECK_RUN",
        ),
        (
            # A valid push run is authoritative even if a spoofed status with the same name exists.
            [run("Svc Check", "dev"), run("Svc Check", "dev", app="some-bot", conclusion="failure")],
            "PASS",
            None,
        ),
    ],
)
def test_check_run_rules(plan_json, runs, result: str, code: str | None) -> None:  # type: ignore[no-untyped-def]
    validated = _impl_levels(plan_json, _with_runs(*runs))["validated"]
    assert validated["result"] == result
    if code is not None:
        assert code in codes(validated["reasons"])


def test_unreachable_merge_commit_fails(plan_json) -> None:  # type: ignore[no-untyped-def]
    snapshot = three_repo_baseline()
    snapshot["repositories"]["svc"]["commits"][I1]["reachableFromIntegrationHead"] = False
    validated = _impl_levels(plan_json, snapshot)["validated"]
    assert validated["result"] == "FAIL"
    assert "COMMIT_NOT_REACHABLE" in codes(validated["reasons"])


@pytest.mark.parametrize(
    ("mapped", "result", "code"),
    [
        ([12, 99], "UNKNOWN", "AMBIGUOUS_PR_MAPPING"),
        ([99], "FAIL", "PR_MAPPING_MISMATCH"),
        ([], "FAIL", "COMMIT_NOT_PR_MERGE"),
        (None, "UNKNOWN", "PR_MAPPING_UNKNOWN"),
    ],
)
def test_merge_commit_must_map_to_exactly_this_pr(plan_json, mapped, result: str, code: str) -> None:  # type: ignore[no-untyped-def]
    snapshot = three_repo_baseline()
    facts = snapshot["repositories"]["svc"]["commits"][I1]
    if mapped is None:
        del facts["mergedPullRequests"]
    else:
        facts["mergedPullRequests"] = mapped
    validated = _impl_levels(plan_json, snapshot)["validated"]
    assert validated["result"] == result
    assert code in codes(validated["reasons"])


@pytest.mark.parametrize(
    ("pulls", "result", "code"),
    [
        ([merged_pr(12, I1, base="main")], "FAIL", "PR_MERGED_INTO_WRONG_BASE"),
        ([merged_pr(12, I1), merged_pr(13, fake_sha("other"))], "UNKNOWN", "AMBIGUOUS_PULL_REQUEST"),
        ([open_pr(12)], "FAIL", "NOT_MERGED"),
        ([], "FAIL", "NOT_MERGED"),
        (
            [{"baseBranch": "dev", "mergeCommitSha": None, "number": 12, "state": "merged"}],
            "UNKNOWN",
            "MERGE_COMMIT_UNKNOWN",
        ),
        ([{"baseBranch": "dev", "mergeCommitSha": None, "number": 12, "state": "closed"}], "FAIL", "NOT_MERGED"),
    ],
)
def test_merged_level_rules(plan_json, pulls, result: str, code: str) -> None:  # type: ignore[no-untyped-def]
    snapshot = three_repo_baseline()
    snapshot["repositories"]["svc"]["branches"]["feat/SVC-IMPL-007-r1"]["pullRequests"] = pulls
    merged = _impl_levels(plan_json, snapshot)["merged"]
    assert merged["result"] == result
    assert code in codes(merged["reasons"])


def test_unlisted_branch_is_unknown_not_unmerged(plan_json) -> None:  # type: ignore[no-untyped-def]
    snapshot = three_repo_baseline()
    del snapshot["repositories"]["svc"]["branches"]["feat/SVC-IMPL-007-r1"]
    merged = _impl_levels(plan_json, snapshot)["merged"]
    assert merged["result"] == "UNKNOWN"
    assert "PR_EVIDENCE_MISSING" in codes(merged["reasons"])


def test_revision_bump_ignores_old_revision_pr(plan_json, tmp_path) -> None:  # type: ignore[no-untyped-def]
    import yaml
    from support import copy_project

    project = copy_project(THREE_REPO, tmp_path)
    path = project / "factory/tasks/SVC-IMPL-007.yaml"
    document = yaml.safe_load(path.read_text())
    document["revision"] = 2
    path.write_text(yaml.safe_dump(document, sort_keys=False))
    snapshot = three_repo_baseline(project)  # approves r2; only the r1 branch has a merged PR
    task = task_of(plan_json(project, snapshot), "SVC-IMPL-007")
    assert task["branch"] == "feat/SVC-IMPL-007-r2"
    assert task["levels"]["merged"]["result"] == "UNKNOWN"
    assert task["state"] == "READY"


@pytest.mark.parametrize(
    ("output", "result", "code"),
    [
        (contract("1.2.0"), "PASS", None),
        (contract(None), "UNKNOWN", "OUTPUT_VERSION_UNKNOWN"),
        (contract("1.2"), "FAIL", "OUTPUT_VERSION_INVALID"),
        (contract(present=False), "FAIL", "OUTPUT_MISSING"),
        ({}, "UNKNOWN", "OUTPUT_EVIDENCE_MISSING"),
    ],
)
def test_declared_outputs_at_head(plan_json, output, result: str, code: str | None) -> None:  # type: ignore[no-untyped-def]
    snapshot = three_repo_baseline()
    snapshot["repositories"]["svc"]["commits"][I1]["outputs"] = output
    ready = _impl_levels(plan_json, snapshot)["integration-ready"]
    assert ready["result"] == result
    if code is not None:
        assert code in codes(ready["reasons"])


def test_missing_integration_head_is_unknown(plan_json) -> None:  # type: ignore[no-untyped-def]
    snapshot = three_repo_baseline()
    del snapshot["repositories"]["svc"]["integrationHead"]
    ready = _impl_levels(plan_json, snapshot)["integration-ready"]
    assert ready["result"] == "UNKNOWN"
    assert {"INTEGRATION_HEAD_UNKNOWN", "OUTPUT_EVIDENCE_MISSING"} <= codes(ready["reasons"])


def test_environment_verified_is_always_unknown(plan_json) -> None:  # type: ignore[no-untyped-def]
    levels = _impl_levels(plan_json, three_repo_baseline())
    assert levels["integration-ready"]["result"] == "PASS"
    assert levels["environment-verified"]["result"] == "UNKNOWN"
    assert codes(levels["environment-verified"]["reasons"]) == {"ENVIRONMENT_EVIDENCE_UNSUPPORTED"}


def test_contract_task_merge_sha_validated_independently_of_head(plan_json) -> None:  # type: ignore[no-untyped-def]
    snapshot = three_repo_baseline()
    snapshot["repositories"]["svc"]["commits"][C1]["checkRuns"] = [run("Svc Check", "dev", conclusion="failure")]
    plan = plan_json(THREE_REPO, snapshot)
    contract_task = task_of(plan, "SVC-CONTRACT-002")
    assert contract_task["levels"]["validated"]["result"] == "FAIL"
    assert contract_task["levels"]["integration-ready"]["result"] == "FAIL"
    # A merged dependency is still merged; only edges requiring more are affected.
    assert task_of(plan, "SVC-IMPL-007")["dependencies"][0]["result"] == "PASS"
    assert task_of(plan, "CLIENT-UI-003")["gates"]["start"]["result"] == "FAIL"
