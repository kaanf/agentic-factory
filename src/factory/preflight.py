"""Dispatch preflight and dry run. Side-effect free: starts no process, writes no file.

``preflight`` answers two separate questions and never mixes them:

1. **Dry run** (``dryRun``): would the adapter build a well-formed, policy-conforming command for
   this task revision, and does the observable state (task, plan, configuration, Archon install
   record, checkout, branches, run records, reconciliation) look consistent? Every check is
   PASS, FAIL or UNKNOWN; the dry-run result is their combination. These checks are informational:
   they read local files and, with ``--live``, GitHub, at one moment.
2. **Authorization** (``authorization``): may anything act on this? In this Factory Core version the
   answer is always no. ``granted`` is computed from a list of unmet preconditions that always
   contains ``EXECUTION_NOT_IMPLEMENTED``, and the other preconditions (enabled dispatch,
   authenticated evidence, verified human approval, credential isolation P1-P3, action-time
   revalidation, an execution ledger) cannot be met by any input either. A dry run that passes,
   a READY task, a simulated snapshot and an unauthenticated one all leave ``granted`` false.

The command is built from the same in-memory project the checks used. Before returning, the manifest
and task files are hashed again; if either changed during preflight, the dry run fails
(``INPUT_CHANGED_DURING_PREFLIGHT``). The output records both hashes and the command digest, so a
future dispatch can revalidate them at action time.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from factory import archon
from factory.archon import ArchonCommand, CommandError
from factory.documents import sha256_file
from factory.fsread import FsError
from factory.github import GitHubApi, GitHubError, path_of
from factory.gitstate import CheckoutState
from factory.model import Project, Task
from factory.planning import Plan, Result, State, combine, task_branch
from factory.project import MANIFEST_PATH
from factory.reconcile import Reconciliation, decide, held, observe, read_local, reconcile_document
from factory.reconcile import State as RunState
from factory.refs import is_command_branch, unsafe_text
from factory.runs import probe_lock

PREFLIGHT_SCHEMA_VERSION = "factory.preflight/v1"

# Preconditions only Stage 6 and human setup can satisfy. Listed in every report.
_STAGE6_PRECONDITIONS: tuple[tuple[str, str], ...] = (
    ("EXECUTION_NOT_IMPLEMENTED", "this Factory Core version has no execution path (no --execute)"),
    (
        "HUMAN_AUTHORIZATION_UNVERIFIABLE",
        "agent and approver credentials are not separated (4B P1-P3), so no approval proves human intent",
    ),
    (
        "ACTION_TIME_REVALIDATION_MISSING",
        "no consumer re-checks heads, approvals and inputs under a lock at the moment of action",
    ),
    (
        "EXECUTION_LEDGER_MISSING",
        "no exclusive dispatch lock and atomic intent/outcome writer exists (Stage 6)",
    ),
)


@dataclass(frozen=True, order=True)
class Check:
    code: str
    result: Result
    message: str
    ref: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "ref": self.ref, "result": self.result.value}


@dataclass(frozen=True)
class Authorization:
    reasons: tuple[Check, ...]

    @property
    def granted(self) -> bool:
        # Structurally false: EXECUTION_NOT_IMPLEMENTED is always among the reasons.
        return not self.reasons

    def to_dict(self) -> dict[str, Any]:
        return {"granted": self.granted, "reasons": [r.to_dict() for r in self.reasons]}


def authorization(project: Project, plan: Plan | None) -> Authorization:
    """Every unmet authorization precondition. Never empty in this Factory Core version."""
    reasons = [Check(code, Result.FAIL, message) for code, message in _STAGE6_PRECONDITIONS]
    if not project.manifest.dispatch.enabled:
        reasons.append(Check("DISPATCH_DISABLED", Result.FAIL, "dispatch.enabled is false in the manifest"))
    if plan is None or not plan.snapshot.authenticated:
        reasons.append(Check("EVIDENCE_NOT_AUTHENTICATED", Result.FAIL, "the evidence is not authenticated"))
    if plan is None or plan.snapshot.provenance != "github":
        provenance = plan.snapshot.provenance if plan is not None else "none"
        reasons.append(
            Check("EVIDENCE_NOT_COLLECTED", Result.FAIL, f"evidence provenance is '{provenance}', not collected")
        )
    return Authorization(tuple(sorted(set(reasons))))


@dataclass(frozen=True)
class Preflight:
    project: Project
    task: Task | None
    checks: tuple[Check, ...]
    command: ArchonCommand | None
    authorization: Authorization
    reconciliation: dict[str, Any] | None
    plan: Plan | None
    inputs: dict[str, str]
    lock_held: bool = False

    @property
    def result(self) -> Result:
        return combine([c.result for c in self.checks])

    def to_dict(self) -> dict[str, Any]:
        failing = [c for c in self.checks if c.result is not Result.PASS]
        evidence = (
            {"authenticated": self.plan.snapshot.authenticated, "provenance": self.plan.snapshot.provenance}
            if self.plan is not None
            else None
        )
        return {
            "authorization": self.authorization.to_dict(),
            "command": None
            if self.command is None
            else {**self.command.to_dict(), "display": self.command.display(), "executed": False},
            "dryRun": {
                "checks": [c.to_dict() for c in self.checks],
                "reasonCodes": sorted({c.code for c in failing}),
                "result": self.result.value,
            },
            "evidence": evidence,
            "inputs": self.inputs,
            "project": self.project.manifest.project_id,
            "reconciliation": self.reconciliation,
            "schemaVersion": PREFLIGHT_SCHEMA_VERSION,
            "task": None
            if self.task is None
            else {
                "file": self.task.file,
                "id": self.task.id,
                "repository": self.task.repository,
                "revision": self.task.revision,
            },
        }


def _fail(code: str, message: str, ref: str = "") -> Check:
    return Check(code, Result.FAIL, message, ref)


def _from(finding: archon.Finding) -> Check:
    return Check(finding.code, Result(finding.result), finding.message, finding.ref)


def _passed(code: str, message: str, ref: str = "") -> Check:
    return Check(code, Result.PASS, message, ref)


def _task_checks(task: Task, plan: Plan | None) -> list[Check]:
    checks = []
    for text, what in [(task.title, "title"), *((item, "acceptance criterion") for item in task.acceptance)]:
        bad = unsafe_text(text)
        if bad is not None:
            checks.append(_fail("TASK_TEXT_UNSAFE", f"task {what} contains control character {bad!r}", task.id))
    if plan is None:
        checks.append(Check("TASK_READINESS_UNKNOWN", Result.UNKNOWN, "no plan was computed"))
        return checks
    task_plan = next((t for t in plan.tasks if t.task.id == task.id), None)
    if task_plan is None:
        checks.append(Check("TASK_READINESS_UNKNOWN", Result.UNKNOWN, "the plan has no entry for this task"))
    elif task_plan.state is State.READY:
        checks.append(
            _passed(
                "TASK_READY",
                f"task is READY in a plan from '{plan.snapshot.provenance}' evidence "
                "(advisory only; it authorizes nothing)",
                task.id,
            )
        )
    else:
        result = Result.UNKNOWN if task_plan.state is State.UNKNOWN else Result.FAIL
        codes = sorted({r.code for r in task_plan.gates["start"].reasons}) or ["-"]
        checks.append(
            Check(
                "TASK_NOT_READY",
                result,
                f"task state is {task_plan.state.value} (start gate: {', '.join(codes)})",
                task.id,
            )
        )
    return checks


def _branch_checks(project: Project, task: Task, branch: str) -> list[Check]:
    repo = project.manifest.repository(task.repository)
    assert repo is not None
    checks = []
    if not is_command_branch(repo.integration_branch):
        checks.append(_fail("INTEGRATION_BRANCH_UNSAFE", f"integration branch {repo.integration_branch!r} is unsafe"))
    if not is_command_branch(branch):
        checks.append(_fail("FEATURE_BRANCH_UNSAFE", f"feature branch {branch!r} is unsafe", branch))
    if branch in (repo.integration_branch, repo.release_branch):
        checks.append(_fail("FEATURE_BRANCH_PROTECTED_NAME", f"feature branch {branch!r} names a protected branch"))
    if repo.integration_branch == repo.release_branch:
        checks.append(_fail("BASE_IS_RELEASE_BRANCH", "the integration branch is also the release branch"))
    return checks


def _checkout_checks(checkout: CheckoutState | None, branch: str) -> list[Check]:
    if checkout is None:
        return []
    checks = []
    if checkout.head_branch == branch:
        checks.append(_fail("CHECKOUT_ON_FEATURE_BRANCH", f"the checkout has {branch} checked out", branch))
    if branch in checkout.branches:
        checks.append(_fail("FEATURE_BRANCH_EXISTS_LOCAL", f"{branch} already exists locally", branch))
    if checkout.worktrees_on(branch):
        checks.append(_fail("FEATURE_WORKTREE_EXISTS", f"a worktree already has {branch} checked out", branch))
    for remote, branches in checkout.remote_branches.items():
        if branch in branches:
            checks.append(
                _fail("REMOTE_TRACKING_BRANCH_EXISTS", f"{remote}/{branch} exists in the checkout's last fetch", branch)
            )
    return checks


def _remote_config(api: GitHubApi | None, project: Project, task: Task) -> list[Check]:
    repo = project.manifest.repository(task.repository)
    assert repo is not None
    if api is None:
        return [
            Check(
                "ARCHON_REMOTE_CONFIG_UNCHECKED",
                Result.UNKNOWN,
                "offline: .archon/config.yaml on the remote integration branch was not read",
            )
        ]
    try:
        body = api.get(f"repos/{repo.github}/contents/.archon/config.yaml", ref=repo.integration_branch)
        if not isinstance(body, dict) or body.get("type") != "file" or body.get("encoding") != "base64":
            raise GitHubError("unexpected contents response")
        text = base64.b64decode(body["content"], validate=False).decode("utf-8")
        problem = None
    except GitHubError as exc:
        text = None
        problem = FsError("NOT_FOUND" if exc.status == 404 else "UNREADABLE", exc.message)
    except (KeyError, TypeError, binascii.Error, UnicodeDecodeError) as exc:
        text, problem = None, FsError("UNREADABLE", f"unreadable contents response: {exc}")
    return [_from(f) for f in archon.check_repo_config(text, problem, repo.integration_branch, "remote")]


def _remote_branch(api: GitHubApi | None, project: Project, task: Task, branch: str) -> list[Check]:
    repo = project.manifest.repository(task.repository)
    assert repo is not None
    if api is None:
        return [
            Check("REMOTE_BRANCH_UNCHECKED", Result.UNKNOWN, f"offline: whether {branch} exists on GitHub is unknown")
        ]
    try:
        refs = api.get(f"repos/{repo.github}/git/matching-refs/heads/{path_of(branch)}")
        if not isinstance(refs, list):
            raise GitHubError("expected a list of refs")
    except GitHubError as exc:
        return [Check("REMOTE_BRANCH_UNKNOWN", Result.UNKNOWN, f"{branch} on GitHub: {exc.message}", branch)]
    if any(isinstance(r, dict) and r.get("ref") == f"refs/heads/{branch}" for r in refs):
        return [_fail("FEATURE_BRANCH_EXISTS_REMOTE", f"{branch} already exists on GitHub", branch)]
    return []


def _reconcile_check(result: Reconciliation) -> list[Check]:
    if result.state is RunState.NO_RUN:
        return []
    kind = Result.UNKNOWN if result.state is RunState.UNKNOWN else Result.FAIL
    codes = sorted({f.code for f in result.findings if f.result != "INFO"})
    return [
        Check(f"RECONCILE_{result.state.value}", kind, f"reconciliation state {result.state.value}: {', '.join(codes)}")
    ]


def _digests(project: Project, task: Task | None) -> dict[str, str]:
    inputs = {"manifestSha256": project.digests.get(MANIFEST_PATH, "")}
    if task is not None:
        inputs["taskSha256"] = project.digests.get(task.file, "")
    return inputs


def _reread(project: Project, task: Task | None) -> list[Check]:
    files = [MANIFEST_PATH] + ([task.file] if task is not None else [])
    for file in files:
        try:
            current = sha256_file(project.root / file)
        except OSError:
            current = None
        if current != project.digests.get(file):
            return [
                _fail("INPUT_CHANGED_DURING_PREFLIGHT", f"{file} changed while preflight ran; nothing is valid", file)
            ]
    return []


def preflight(
    project: Project,
    task_id: str,
    revision: int,
    checkout: Path,
    state_root: Path,
    archon_home: Path,
    plan: Plan | None,
    api: GitHubApi | None,
) -> Preflight:
    """Run every dry-run check for one task revision. The project must have passed validation."""
    task = project.tasks.get(task_id)
    if task is None:
        checks = (_fail("TASK_NOT_FOUND", f"no task '{task_id}' in the project", task_id),)
        return Preflight(project, None, checks, None, authorization(project, plan), None, plan, _digests(project, None))
    repo = project.manifest.repository(task.repository)
    assert repo is not None
    if probe_lock(state_root, project.manifest.project_id) == "held":
        # A second coordinator refuses at once: nothing else is read and nothing is requested.
        observations = held(project, task)
        result = decide(observations)
        return Preflight(
            project,
            task,
            tuple(_reconcile_check(result)),
            None,
            authorization(project, plan),
            reconcile_document(task, repo, result, observations),
            plan,
            _digests(project, task),
            lock_held=True,
        )
    checks: list[Check] = []
    if task.revision != revision:
        checks.append(
            _fail("TASK_REVISION_MISMATCH", f"task {task.id} is at revision {task.revision}, not {revision}", task.id)
        )
    branch = task_branch(repo, task)
    dispatch = project.manifest.dispatch

    checks.extend(_task_checks(task, plan))
    checks.extend(_branch_checks(project, task, branch))
    workflow, findings = archon.resolve_workflow(task, dispatch)
    checks.extend(_from(f) for f in findings)
    _, assistant, findings = archon.resolve_provider(task, dispatch)
    checks.extend(_from(f) for f in findings)
    install = archon.read_archon_home(archon_home)
    checks.extend(_from(f) for f in archon.check_archon_version(dispatch, install.version, install.version_problem))
    checks.extend(_from(f) for f in archon.check_assistant(assistant, install))

    local, local_findings = read_local(checkout, repo)
    checks.extend(Check(f.code, Result.UNKNOWN if f.result == "UNKNOWN" else Result.FAIL, f.message, f.ref)
                  for f in local_findings if f.result != "INFO")  # fmt: skip
    checks.extend(_checkout_checks(local, branch))
    text, problem = archon.read_local_repo_config(checkout)
    checks.extend(_from(f) for f in archon.check_repo_config(text, problem, repo.integration_branch, "local"))
    checks.extend(_remote_config(api, project, task))
    checks.extend(_remote_branch(api, project, task, branch))

    observations = observe(project, task, checkout, state_root, api, local, local_findings)
    result = decide(observations)
    lock_held = result.state is RunState.COORDINATOR_ACTIVE
    checks.extend(_reconcile_check(result))
    reconciliation = reconcile_document(task, repo, result, observations)

    command: ArchonCommand | None = None
    # A command is only shown for the revision the caller asked about.
    if workflow is not None and task.revision == revision:
        try:
            command = archon.build_command(task, repo, branch, workflow, str(checkout))
        except CommandError as exc:
            checks.append(_fail(exc.code, exc.message))
    if command is not None:
        checks.append(_passed("COMMAND_BUILT", "a command was built (dry run: not executed)"))
    checks.extend(_reread(project, task))
    return Preflight(
        project,
        task,
        tuple(sorted(set(checks))),
        command,
        authorization(project, plan),
        reconciliation,
        plan,
        _digests(project, task),
        lock_held,
    )
