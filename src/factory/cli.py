"""Command-line interface. Never writes to the project directory.

``validate`` and ``plan --evidence`` read only local files. ``evidence collect`` and ``plan --live``
additionally perform read-only GitHub API requests through the GitHub CLI.

Exit codes:
    0   success (validate: project is valid; plan: plan computed, whatever the task states, and with
        --live every fact collected; evidence collect: every fact collected)
    2   invalid input (project or evidence snapshot failed validation)
    3   evidence incomplete (snapshot or live plan written, but some facts could not be collected and
        evaluate as UNKNOWN), or GitHub CLI unusable (nothing written)
    64  usage error
    70  internal error
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import NoReturn, TextIO

from factory import __version__
from factory.collector import CollectionError, collect
from factory.evidence import load_snapshot
from factory.github import GhCliTransport, GitHubApi, GitHubError, Transport
from factory.issues import Issue
from factory.live import plan_live
from factory.model import Project
from factory.planning import Plan, evaluate
from factory.render import plan_text, to_json, validation_document
from factory.validation import validate_project

EXIT_OK = 0
EXIT_INVALID = 2
EXIT_INCOMPLETE = 3
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

    plan = commands.add_parser("plan", help="evaluate task readiness against evidence (advisory, read-only)")
    plan.add_argument("project", type=Path, help="project directory (planning repository root)")
    source = plan.add_mutually_exclusive_group(required=True)
    source.add_argument("--evidence", type=Path, help="evidence snapshot JSON file (offline)")
    source.add_argument("--live", action="store_true", help="collect evidence from GitHub now (read-only)")
    plan.add_argument(
        "--save-evidence", type=Path, metavar="FILE", help="with --live: also write the snapshot the plan used"
    )
    plan.add_argument("--format", choices=("text", "json"), default="text")

    evidence = commands.add_parser("evidence", help="evidence snapshot commands")
    evidence_commands = evidence.add_subparsers(dest="evidence_command", required=True, parser_class=_Parser)
    collect_cmd = evidence_commands.add_parser("collect", help="collect a snapshot from GitHub (read-only)")
    collect_cmd.add_argument("project", type=Path, help="project directory (planning repository root)")
    collect_cmd.add_argument("--output", "-o", type=Path, help="write the snapshot here instead of stdout")
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


def _inside_project(path: Path | None, project: Project) -> bool:
    return path is not None and path.resolve().is_relative_to(project.root)


def _report_collection(errors: list[CollectionError], isolation: dict[str, str], err: TextIO) -> None:
    err.write(f"warning: human authorization unverifiable: {isolation['message']}\n")
    for error in errors:
        err.write(f"incomplete: {error.render()}\n")
    if errors:
        err.write(f"{len(errors)} fact(s) could not be collected; they will evaluate as UNKNOWN\n")


def _plan(
    args: argparse.Namespace, out: TextIO, err: TextIO, transport: Transport | None, now: Callable[[], datetime] | None
) -> int:
    if args.save_evidence is not None and not args.live:
        err.write("factory: error: --save-evidence requires --live\n")
        return EXIT_USAGE
    result = validate_project(args.project)
    project = result.project
    project_id = project.manifest.project_id if project else None
    if result.errors:
        return _report_invalid(result.errors, result.warnings, args.format, project_id, out, err)
    assert project is not None

    collection_errors: list[CollectionError] = []
    if args.live:
        if _inside_project(args.save_evidence, project):
            err.write("factory: error: --save-evidence must not be inside the project directory (it is read-only)\n")
            return EXIT_USAGE
        try:
            api = GitHubApi(transport or GhCliTransport())
        except GitHubError as exc:
            err.write(f"error: {exc.message}\n")
            return EXIT_INCOMPLETE
        live = plan_live(project, api, now)
        if live.issues:
            return _report_invalid(live.issues, [], args.format, project_id, out, err)
        assert live.plan is not None
        if args.save_evidence is not None:
            args.save_evidence.write_text(to_json(live.snapshot), encoding="utf-8")
        plan: Plan = live.plan
        collection_errors = live.errors
        _report_collection(collection_errors, live.snapshot["credentialIsolation"], err)
    else:
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
    return EXIT_INCOMPLETE if collection_errors else EXIT_OK


def _collect(
    args: argparse.Namespace, out: TextIO, err: TextIO, transport: Transport | None, now: Callable[[], datetime] | None
) -> int:
    result = validate_project(args.project)
    project = result.project
    if result.errors:
        return _report_invalid(result.errors, result.warnings, "text", None, out, err)
    assert project is not None
    if _inside_project(args.output, project):
        err.write("factory: error: --output must not be inside the project directory (it is read-only)\n")
        return EXIT_USAGE
    try:
        api = GitHubApi(transport or GhCliTransport())
    except GitHubError as exc:
        err.write(f"error: {exc.message}\n")
        return EXIT_INCOMPLETE
    collection = collect(project, api, now)
    document = to_json(collection.snapshot)
    if args.output is None:
        out.write(document)
    else:
        args.output.write_text(document, encoding="utf-8")
    _report_collection(collection.errors, collection.snapshot["credentialIsolation"], err)
    return EXIT_INCOMPLETE if collection.errors else EXIT_OK


def run(
    argv: Sequence[str] | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
    transport: Transport | None = None,
    now: Callable[[], datetime] | None = None,
) -> int:
    out = out or sys.stdout
    err = err or sys.stderr
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else EXIT_USAGE
    try:
        if args.command == "validate":
            return _validate(args, out, err)
        if args.command == "evidence":
            return _collect(args, out, err, transport, now)
        return _plan(args, out, err, transport, now)
    except Exception as exc:  # noqa: BLE001 - last-resort guard; report and fail closed
        err.write(f"internal error: {type(exc).__name__}: {exc}\n")
        return EXIT_INTERNAL
