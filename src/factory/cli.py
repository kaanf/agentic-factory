"""Command-line interface. Reads project files and an evidence snapshot; writes only to stdout/stderr.

Exit codes:
    0   success (validate: project is valid; plan: plan computed, whatever the task states)
    2   invalid input (project or evidence snapshot failed validation)
    64  usage error
    70  internal error
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import NoReturn, TextIO

from factory import __version__
from factory.evidence import load_snapshot
from factory.issues import Issue
from factory.planning import evaluate
from factory.render import plan_text, to_json, validation_document
from factory.validation import validate_project

EXIT_OK = 0
EXIT_INVALID = 2
EXIT_USAGE = 64
EXIT_INTERNAL = 70


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: error: {message}\n")


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="factory", description="Factory Core: offline task-graph validation and dependency planning.")
    parser.add_argument("--version", action="version", version=f"factory {__version__}")
    commands = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)

    validate = commands.add_parser("validate", help="validate a project directory")
    validate.add_argument("project", type=Path, help="project directory (planning repository root)")
    validate.add_argument("--format", choices=("text", "json"), default="text")

    plan = commands.add_parser("plan", help="evaluate task readiness against an evidence snapshot")
    plan.add_argument("project", type=Path, help="project directory (planning repository root)")
    plan.add_argument("--evidence", type=Path, required=True, help="evidence snapshot JSON file")
    plan.add_argument("--format", choices=("text", "json"), default="text")
    return parser


def _report_invalid(
    errors: list[Issue], warnings: list[Issue], fmt: str, project_id: str | None, out: TextIO, err: TextIO
) -> int:
    if fmt == "json":
        out.write(to_json(validation_document(project_id, errors, warnings, {})))
    else:
        for issue in [*errors, *warnings]:
            prefix = "error" if issue in errors else "warning"
            err.write(f"{prefix}: {issue.render()}\n")
        err.write(f"{len(errors)} error(s), {len(warnings)} warning(s)\n")
    return EXIT_INVALID


def _validate(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    result = validate_project(args.project)
    project = result.project
    project_id = project.manifest.project_id if project else None
    if result.errors:
        return _report_invalid(result.errors, result.warnings, args.format, project_id, out, err)
    assert project is not None
    counts = {
        "adrs": len(project.adrs),
        "profiles": len(project.profiles),
        "repositories": len(project.manifest.repositories),
        "tasks": len(project.tasks),
    }
    if args.format == "json":
        out.write(to_json(validation_document(project_id, [], result.warnings, counts)))
    else:
        for warning in result.warnings:
            err.write(f"warning: {warning.render()}\n")
        out.write(
            f"OK: project '{project_id}' is valid "
            f"({counts['repositories']} repositories, {counts['tasks']} tasks, "
            f"{counts['profiles']} profiles, {counts['adrs']} ADRs)\n"
        )
    return EXIT_OK


def _plan(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    result = validate_project(args.project)
    project = result.project
    project_id = project.manifest.project_id if project else None
    if result.errors:
        return _report_invalid(result.errors, result.warnings, args.format, project_id, out, err)
    assert project is not None
    snapshot_result = load_snapshot(args.evidence, project)
    if snapshot_result.errors:
        return _report_invalid(snapshot_result.errors, [], args.format, project_id, out, err)
    assert snapshot_result.snapshot is not None
    plan = evaluate(project, snapshot_result.snapshot)
    if args.format == "json":
        out.write(to_json(plan.to_dict()))
    else:
        for warning in result.warnings:
            err.write(f"warning: {warning.render()}\n")
        out.write(plan_text(plan))
    return EXIT_OK


def run(argv: Sequence[str] | None = None, out: TextIO | None = None, err: TextIO | None = None) -> int:
    out = out or sys.stdout
    err = err or sys.stderr
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else EXIT_USAGE
    try:
        if args.command == "validate":
            return _validate(args, out, err)
        return _plan(args, out, err)
    except Exception as exc:  # noqa: BLE001 - last-resort guard; report and fail closed
        err.write(f"internal error: {type(exc).__name__}: {exc}\n")
        return EXIT_INTERNAL
