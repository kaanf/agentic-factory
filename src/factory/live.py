"""Live planning: collect evidence from GitHub, validate it and plan, in one read-only operation.

Composition only. ``collect`` gathers repository facts (Stage 2) and approval provenance (Stage 3),
``parse_snapshot`` applies the same validation a snapshot file gets, and ``evaluate`` plans. Nothing
here judges evidence, and nothing here can write to GitHub or to the project.

Collection is not atomic. Each repository's facts, and the approvals, are read in separate windows,
each guarded by reading its integration head before and after. Once everything is collected, every
recorded integration head is read once more. A head that moved, or cannot be read, loses the facts
anchored to it, exactly as when it moves during collection: the head itself and every reachability
fact. Approvals are kept only while the planning repository's head is recorded and still current.
This narrows the gap between phases; it does not prove that a head never moved and moved back, so a
consumer that acts on a plan must revalidate the revisions it acts on.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from factory.collector import CollectionError, collect, read_integration_head
from factory.evidence import parse_snapshot
from factory.github import GitHubApi, GitHubError
from factory.issues import Issue
from factory.model import Project
from factory.planning import Plan, evaluate

LIVE_SOURCE = "<live collection>"


@dataclass(frozen=True)
class LiveResult:
    """``plan`` is None exactly when ``issues`` is non-empty (the collected snapshot is invalid).

    ``snapshot`` is the document the plan was computed from; ``errors`` lists every fact that could
    not be collected or confirmed, which the plan evaluates as UNKNOWN.
    """

    snapshot: dict[str, Any]
    errors: list[CollectionError]
    issues: list[Issue]
    plan: Plan | None


def _confirm_heads(project: Project, api: GitHubApi, snapshot: dict[str, Any]) -> list[CollectionError]:
    """Re-read every recorded integration head and drop facts anchored to one that is not current."""
    errors: list[CollectionError] = []
    for repo_id, facts in sorted(snapshot["repositories"].items()):
        recorded = facts.get("integrationHead")
        repo = project.manifest.repository(repo_id)
        if recorded is None or repo is None:
            continue
        try:
            current = read_integration_head(api, repo)
            problem = f"moved from {recorded} to {current} after it was collected"
        except GitHubError as exc:
            current, problem = None, f"could not be confirmed after collection: {exc.message}"
        if current == recorded:
            continue
        errors.append(CollectionError(repo_id, "integrationHead", problem))
        del facts["integrationHead"]
        for commit in facts.get("commits", {}).values():
            commit.pop("reachableFromIntegrationHead", None)

    planning = project.manifest.planning_repository
    if snapshot["approvals"] and "integrationHead" not in snapshot["repositories"].get(planning, {}):
        errors.append(
            CollectionError(
                planning,
                "approvals",
                "the planning repository's integration head is not confirmed for the whole collection, "
                "so approval provenance cannot be tied to it",
            )
        )
        snapshot["approvals"] = []
    return errors


def _not_collected(snapshot: dict[str, Any]) -> list[Issue]:
    """A live plan must rest on collected evidence: never on a hand-written or simulated document."""
    if snapshot["provenance"]["kind"] != "github" or "collection" not in snapshot:
        return [
            Issue(
                LIVE_SOURCE,
                "provenance",
                "LIVE_EVIDENCE_NOT_COLLECTED",
                "live planning requires a snapshot written by the GitHub collector (provenance 'github' with a "
                "collection record)",
            )
        ]
    return []


def plan_live(project: Project, api: GitHubApi, now: Callable[[], datetime] | None = None) -> LiveResult:
    """Collect, confirm, validate and evaluate. The project must have passed validation."""
    collection = collect(project, api, now)
    snapshot = copy.deepcopy(collection.snapshot)
    checked = parse_snapshot(snapshot, project, LIVE_SOURCE)
    issues = checked.errors or _not_collected(snapshot)
    if issues:
        return LiveResult(snapshot, list(collection.errors), issues, None)

    errors = sorted([*collection.errors, *_confirm_heads(project, api, snapshot)])
    snapshot["collection"] = {**snapshot["collection"], "complete": not errors, "errors": [e.to_dict() for e in errors]}
    final = parse_snapshot(snapshot, project, LIVE_SOURCE)
    if final.errors:
        return LiveResult(snapshot, errors, final.errors, None)
    assert final.snapshot is not None
    return LiveResult(snapshot, errors, [], evaluate(project, final.snapshot))
