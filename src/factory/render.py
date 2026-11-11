"""Deterministic JSON and human-readable rendering."""

from __future__ import annotations

import json
from typing import Any

from factory.issues import Issue
from factory.planning import Plan, Reason, Result


def to_json(document: Any) -> str:
    """Canonical JSON: sorted keys, fixed indentation, ASCII only, trailing newline."""
    return json.dumps(document, sort_keys=True, indent=2, ensure_ascii=True) + "\n"


def validation_document(
    project_id: str | None, errors: list[Issue], warnings: list[Issue], counts: dict[str, int]
) -> dict[str, Any]:
    return {
        "counts": counts,
        "errors": [e.to_dict() for e in errors],
        "project": project_id,
        "schemaVersion": "factory.validation/v1",
        "valid": not errors,
        "warnings": [w.to_dict() for w in warnings],
    }


def _reason_lines(reason: Reason, depth: int) -> list[str]:
    indent = "    " * depth
    ref = f" [{reason.ref}]" if reason.ref else ""
    lines = [f"{indent}- {reason.code} ({reason.result.value}){ref}: {reason.message}"]
    for cause in reason.causes:
        lines.extend(_reason_lines(cause, depth + 1))
    return lines


def plan_text(plan: Plan) -> str:
    data = plan.to_dict()
    evidence = data["evidence"]
    lines = [f"Project: {data['project']}"]
    authenticity = (
        "authenticated"
        if evidence["authenticated"]
        else "NOT authenticated: approvals are not verified human approvals"
    )
    lines.append(f"Evidence: {evidence['provenance']} ({authenticity})")
    if evidence["observedAt"]:
        lines.append(f"Observed at: {evidence['observedAt']}")
    if "collection" in evidence:
        missing = len(evidence["collection"]["errors"])
        lines.append(
            "Collection: complete"
            if evidence["collection"]["complete"] and not missing
            else f"Collection: INCOMPLETE ({missing} fact(s) could not be collected and evaluate as UNKNOWN)"
        )
    if "credentialIsolation" in evidence:
        status = evidence["credentialIsolation"]["status"]
        lines.append(f"Human authorization: unverifiable (credential isolation: {status})")
    lines.append("")

    header = ("TASK", "REPO", "STATE", "LEVEL", "START", "OPEN-PR", "MERGE", "COMPLETE")
    rows = [
        (
            t.task.id,
            t.task.repository,
            t.state.value,
            t.achieved_level or "-",
            t.gates["start"].result.value,
            t.gates["openPullRequest"].result.value,
            t.gates["merge"].result.value,
            t.gates["complete"].result.value,
        )
        for t in plan.tasks
    ]
    widths = [max(len(str(row[i])) for row in [header, *rows]) for i in range(len(header))]
    for row in [header, *rows]:
        lines.append("  ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
    lines.append("")
    lines.append("Summary: " + ", ".join(f"{state} {count}" for state, count in data["summary"].items() if count))

    for task_plan in plan.tasks:
        start = task_plan.gates["start"]
        merge = task_plan.gates["merge"]
        complete = task_plan.gates["complete"]
        start_keys = {r.sort_key() for r in start.reasons}
        merge_extra = [r for r in merge.reasons if r.sort_key() not in start_keys]
        sections: list[tuple[str, list[Reason]]] = []
        if task_plan.stale:
            sections.append(("stale", list(task_plan.stale)))
        if start.result is not Result.PASS:
            sections.append((f"start {start.result.value}", [r for r in start.reasons if r not in task_plan.stale]))
        if merge.result is not Result.PASS and merge_extra:
            sections.append((f"merge {merge.result.value}, in addition to start", merge_extra))
        if complete.result is not Result.PASS:
            sections.append(
                (f"complete {complete.result.value}", [r for r in complete.reasons if r not in task_plan.stale])
            )
        sections = [(title, reasons) for title, reasons in sections if reasons]
        if not sections:
            continue
        lines.append("")
        lines.append(
            f"{task_plan.task.id} (revision {task_plan.task.revision}, {task_plan.state.value}, branch {task_plan.branch})"
        )
        for title, reasons in sections:
            lines.append(f"  {title}:")
            for reason in reasons:
                lines.extend(_reason_lines(reason, 1))
    return "\n".join(lines) + "\n"
