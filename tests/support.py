"""Builders for evidence snapshots used by tests and by the committed snapshot fixtures.

Every approval in these snapshots is SIMULATED. They model what a future GitHub collector
would report; they are not authenticated human approvals.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

from factory.project import load_project

FIXTURES = Path(__file__).parent / "fixtures"
THREE_REPO = FIXTURES / "three-repo-project"
SINGLE_REPO = FIXTURES / "single-repo-project"
EVIDENCE = FIXTURES / "evidence"

CI = ".github/workflows/ci.yml"


def fake_sha(label: str) -> str:
    """A deterministic 40-hex commit id for a readable label."""
    return hashlib.sha1(label.encode()).hexdigest()  # noqa: S324 - not used for security


C1 = fake_sha("svc contract merge")
I1 = fake_sha("svc implementation merge")
P0 = fake_sha("plan head")
K0 = fake_sha("client head")
T1 = fake_sha("tool config merge")
T2 = fake_sha("tool report merge")


def copy_project(source: Path, destination: Path) -> Path:
    target = destination / source.name
    shutil.copytree(source, target)
    return target


def approvals(root: Path, approver: str) -> list[dict[str, Any]]:
    """Simulated approval of every task, profile and ADR at its current content."""
    project = load_project(root).project
    assert project is not None, "fixture project must load"
    files = (
        [t.file for t in project.tasks.values()]
        + [p.file for p in project.profiles.values()]
        + [a.file for a in project.adrs.values()]
    )
    return [
        {"approved": True, "mergedBy": approver, "path": f, "pullRequest": 1, "sha256": project.digests[f]}
        for f in sorted(files)
    ]


def run(
    name: str,
    branch: str,
    *,
    workflow: str = CI,
    event: str = "push",
    conclusion: str | None = "success",
    status: str = "completed",
    app: str = "github-actions",
    attempt: int = 1,
) -> dict[str, Any]:
    return {
        "app": app,
        "attempt": attempt,
        "conclusion": conclusion,
        "event": event,
        "headBranch": branch,
        "name": name,
        "status": status,
        "workflowPath": workflow,
    }


def merged_pr(number: int, sha: str, base: str = "dev") -> dict[str, Any]:
    return {"baseBranch": base, "mergeCommitSha": sha, "number": number, "state": "merged"}


def open_pr(number: int, base: str = "dev") -> dict[str, Any]:
    return {"baseBranch": base, "mergeCommitSha": None, "number": number, "state": "open"}


def commit(pr: int, runs: list[dict[str, Any]], outputs: dict[str, Any] | None = None) -> dict[str, Any]:
    facts: dict[str, Any] = {"checkRuns": runs, "mergedPullRequests": [pr], "reachableFromIntegrationHead": True}
    if outputs is not None:
        facts["outputs"] = outputs
    return facts


def contract(version: str | None = "1.2.0", present: bool = True) -> dict[str, Any]:
    return {"svc-contract": {"present": present, "version": version}}


def three_repo_baseline(root: Path = THREE_REPO) -> dict[str, Any]:
    """Contract and implementation merged and validated on svc; client UI in review."""
    return {
        "approvals": approvals(root, "example-human"),
        "observedAt": "2026-11-11T12:00:00Z",
        "project": "example-three",
        "provenance": {"kind": "simulated", "note": "test fixture; approvals are not authenticated"},
        "repositories": {
            "client": {
                "branches": {
                    "feat/CLIENT-DOCS-005-r1": {"pullRequests": []},
                    "feat/CLIENT-INT-004-r1": {"pullRequests": []},
                    "feat/CLIENT-UI-003-r1": {"pullRequests": [open_pr(21)]},
                },
                "commits": {K0: commit(1, [run("Client Check", "dev")])},
                "integrationHead": K0,
            },
            "plan": {
                "branches": {"feat/PLAN-SPIKE-001-r1": {"pullRequests": []}},
                "commits": {P0: commit(5, [run("Plan Check", "dev")])},
                "integrationHead": P0,
            },
            "svc": {
                "branches": {
                    "feat/SVC-CONTRACT-002-r1": {"pullRequests": [merged_pr(11, C1)]},
                    "feat/SVC-IMPL-007-r1": {"pullRequests": [merged_pr(12, I1)]},
                },
                "commits": {
                    C1: commit(11, [run("Svc Check", "dev")], contract()),
                    I1: commit(12, [run("Svc Check", "dev")], contract()),
                },
                "integrationHead": I1,
            },
        },
        "schemaVersion": "factory.evidence-snapshot/v1",
    }


def three_repo_merged_without_push_ci(root: Path = THREE_REPO) -> dict[str, Any]:
    """The implementation PR is merged, but only pull_request CI ran; nothing ran on the merge commit."""
    snapshot = three_repo_baseline(root)
    svc = snapshot["repositories"]["svc"]
    svc["commits"][I1]["checkRuns"] = [run("Svc Check", "feat/SVC-IMPL-007-r1", event="pull_request")]
    return snapshot


def single_repo_baseline(root: Path = SINGLE_REPO) -> dict[str, Any]:
    checks = [
        run("pytest", "trunk", workflow=".github/workflows/test.yml"),
        run("import-linter", "trunk", workflow=".github/workflows/architecture.yml"),
    ]
    return {
        "approvals": approvals(root, "solo-maintainer"),
        "project": "solo-tool",
        "provenance": {"kind": "simulated"},
        "repositories": {
            "tool": {
                "branches": {
                    "topic/TOOL-1-r3": {"pullRequests": [merged_pr(3, T1, base="trunk")]},
                    "topic/TOOL-2-r1": {"pullRequests": [merged_pr(4, T2, base="trunk")]},
                    "topic/TOOL-3-r1": {"pullRequests": []},
                },
                "commits": {T1: commit(3, checks), T2: commit(4, checks)},
                "integrationHead": T2,
            }
        },
        "schemaVersion": "factory.evidence-snapshot/v1",
    }


STATIC_SNAPSHOTS = {
    "three-repo-baseline.json": three_repo_baseline,
    "three-repo-merged-without-push-ci.json": three_repo_merged_without_push_ci,
    "single-repo-baseline.json": single_repo_baseline,
}


def write_snapshot(document: dict[str, Any], path: Path) -> Path:
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


if __name__ == "__main__":
    # Regenerate committed snapshot fixtures: python tests/support.py
    EVIDENCE.mkdir(exist_ok=True)
    for name, builder in STATIC_SNAPSHOTS.items():
        write_snapshot(builder(), EVIDENCE / name)
        print(f"wrote {EVIDENCE / name}")
