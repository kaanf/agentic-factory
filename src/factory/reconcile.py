"""Read-only reconciliation of one task revision (Stage 4B §7).

``observe`` gathers facts; ``decide`` is a pure function from facts to one state. Nothing here
writes anything: no run records, no branches, no pull requests, no worktrees. ``--adopt`` and
``--abandon`` are not implemented; the states below say which human action each one needs.

Facts, per revision of the task (the current one and every older one):

- run records under the runtime state directory (``runs``),
- the local branch and any linked worktree on it, read from the checkout's git metadata
  (``gitstate``),
- the remote branch and the pull requests whose head is that branch, read from GitHub (read-only),
- Archon's own run status, which Stage 5 does not read (its store has no documented read
  interface); it is always reported as not observed.

A fact that could not be read is None and makes the result UNKNOWN unless something already makes
it UNCERTAIN. Facts that contradict each other make it UNCERTAIN. Neither is ever a success.

Only one coordinator host is supported (4B D10). Records written on another host are reported, and
nothing here can see work started from another host that left no record on this one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from factory.collector import branch_pull_requests
from factory.github import GitHubApi, GitHubError, path_of
from factory.gitstate import ABSENT, PRESENT, CheckoutState, GitStateError, github_slug, read_checkout
from factory.model import Project, Repository, Task
from factory.planning import task_branch
from factory.refs import is_branch, is_object_id
from factory.runs import RevisionRecords, TaskRecords, probe_lock, read_task_records

RECONCILE_SCHEMA_VERSION = "factory.reconcile/v1"
# Older revisions whose pull requests are looked up. Beyond this the history is UNKNOWN.
MAX_HISTORY = 50


class State(StrEnum):
    COORDINATOR_ACTIVE = "COORDINATOR_ACTIVE"
    UNCERTAIN = "UNCERTAIN"
    UNKNOWN = "UNKNOWN"
    SUPERSEDED_PR_OPEN = "SUPERSEDED_PR_OPEN"
    UNTRACKED_BRANCH = "UNTRACKED_BRANCH"
    UNTRACKED_LOCAL_BRANCH = "UNTRACKED_LOCAL_BRANCH"
    PR_MERGED = "PR_MERGED"
    PR_OPEN = "PR_OPEN"
    PR_CLOSED = "PR_CLOSED"
    NO_PR = "NO_PR"
    RUN_FAILED = "RUN_FAILED"
    ABANDONED = "ABANDONED"
    ATTEMPTS_EXHAUSTED = "ATTEMPTS_EXHAUSTED"
    NO_RUN = "NO_RUN"


HUMAN_ACTION: dict[State, str] = {
    State.COORDINATOR_ACTIVE: "None: another coordinator holds the dispatch lock. Run reconcile again after it exits.",
    State.UNCERTAIN: "Inspect the listed facts. For an interrupted dispatch, wait for or stop the Archon run "
    "(archon workflow runs), then record adopt or abandon (manual until Stage 6).",
    State.UNKNOWN: "Make the missing facts readable (GitHub access, checkout, state directory) and reconcile again.",
    State.SUPERSEDED_PR_OPEN: "Close the older revision's pull request, or revert the revision bump. "
    "The older pull request never counts as evidence for this revision.",
    State.UNTRACKED_BRANCH: "Decide whether the branch is legitimate work for this revision; adopting it is "
    "manual until Stage 6. Otherwise remove it yourself.",
    State.UNTRACKED_LOCAL_BRANCH: "Inspect the local branch or worktree; finish it, or remove it and the worktree "
    "yourself (archon complete).",
    State.PR_MERGED: "None: this revision is merged. Readiness and evidence come from factory plan.",
    State.PR_OPEN: "Review the pull request.",
    State.PR_CLOSED: "The pull request was closed without merging. Revise the task or remove the branch yourself.",
    State.NO_PR: "The run produced no pull request. Open one from the branch yourself, or abandon the attempt "
    "and revise the task. It is not retried.",
    State.RUN_FAILED: "The run failed. It counts as an attempt and is not retried; revise the task or decide "
    "on another attempt.",
    State.ABANDONED: "The attempt was abandoned and counts. Remove its branch before any further attempt.",
    State.ATTEMPTS_EXHAUSTED: "Every allowed attempt for this revision is used. Revise the task (new revision).",
    State.NO_RUN: "None: nothing has been dispatched for this revision.",
}

# States in which reconcile itself sees nothing that blocks a first or next attempt. This is not an
# authorization: Stage 5 cannot dispatch, and Stage 6 must re-check everything under its lock.
_NOT_BLOCKING = frozenset({State.NO_RUN})


@dataclass(frozen=True, order=True)
class Finding:
    code: str
    result: str  # UNCERTAIN, UNKNOWN, BLOCKING or INFO
    message: str
    ref: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "ref": self.ref, "result": self.result}


@dataclass(frozen=True)
class Pull:
    number: int
    state: str  # open, closed or merged
    base: str


@dataclass(frozen=True)
class RevisionFacts:
    revision: int
    branch: str
    records: RevisionRecords | None  # None: state directory unreadable
    local: bool | None
    worktree: bool | None
    remote: bool | None
    pulls: tuple[Pull, ...] | None


@dataclass(frozen=True)
class Observations:
    project: str
    task: str
    revision: int
    integration: str
    attempt_cap: int
    lock: str  # absent, free, held, unknown
    current: RevisionFacts
    older: tuple[RevisionFacts, ...]
    newer_revisions: tuple[int, ...]
    findings: tuple[Finding, ...]  # problems found while observing


@dataclass(frozen=True)
class Reconciliation:
    state: State
    findings: tuple[Finding, ...]
    history: tuple[Finding, ...]
    attempts_used: int
    attempt_cap: int

    @property
    def blocks_dispatch(self) -> bool:
        return self.state not in _NOT_BLOCKING

    def to_dict(self) -> dict[str, Any]:
        return {
            "attemptCap": self.attempt_cap,
            "attemptsUsed": self.attempts_used,
            "blocksDispatch": self.blocks_dispatch,
            "findings": [f.to_dict() for f in self.findings],
            "history": [f.to_dict() for f in self.history],
            "humanAction": HUMAN_ACTION[self.state],
            "state": self.state.value,
        }


# -- decision ------------------------------------------------------------------------------------


def _uncertain(code: str, message: str, ref: str = "") -> Finding:
    return Finding(code, "UNCERTAIN", message, ref)


def _unknown(code: str, message: str, ref: str = "") -> Finding:
    return Finding(code, "UNKNOWN", message, ref)


def _info(code: str, message: str, ref: str = "") -> Finding:
    return Finding(code, "INFO", message, ref)


def _record_findings(facts: RevisionFacts) -> list[Finding]:
    ref = facts.branch
    if facts.records is None:
        return [_unknown("RUN_RECORDS_UNREADABLE", f"run records for revision {facts.revision} are unreadable", ref)]
    findings = [
        _uncertain(p.code, f"revision {p.revision} attempt {p.attempt}: {p.message}", ref)
        for p in facts.records.problems
    ]
    last = facts.records.last
    if last is not None and last.interrupted:
        findings.append(
            _uncertain(
                "INTERRUPTED_DISPATCH",
                f"revision {facts.revision} attempt {last.number} has an intent and no outcome: the dispatch was "
                "interrupted or is still running (Archon may continue as an orphan)",
                ref,
            )
        )
        findings.append(
            _info("ARCHON_RUN_STATUS_NOT_OBSERVED", "Archon's run status is not read by Factory Core; check it", ref)
        )
    return findings


def _pull_findings(facts: RevisionFacts, integration: str) -> list[Finding]:
    if facts.pulls is None:
        return [_unknown("PULL_REQUESTS_UNKNOWN", f"pull requests from {facts.branch} could not be read", facts.branch)]
    findings = []
    for pull in facts.pulls:
        if pull.base != integration:
            findings.append(
                _uncertain(
                    "PR_WRONG_BASE",
                    f"PR #{pull.number} from {facts.branch} targets '{pull.base}', not '{integration}'",
                    facts.branch,
                )
            )
    merged = [p for p in facts.pulls if p.state == "merged"]
    active = [p for p in facts.pulls if p.state in ("open", "merged")]
    if len(merged) > 1 or len(active) > 1:
        numbers = ", ".join(f"#{p.number}" for p in active)
        findings.append(
            _uncertain("AMBIGUOUS_PULL_REQUESTS", f"several pull requests from {facts.branch}: {numbers}", facts.branch)
        )
    return findings


def _attempts_used(records: RevisionRecords | None) -> int:
    return len(records.attempts) if records is not None else 0


def decide(obs: Observations) -> Reconciliation:
    """Map observations to one state. Pure; every applicable finding is reported."""
    current = obs.current
    used = _attempts_used(current.records)
    if obs.lock == "held":
        finding = Finding(
            "COORDINATOR_LOCK_HELD", "BLOCKING", "another coordinator holds the dispatch lock; nothing was evaluated"
        )
        return Reconciliation(State.COORDINATOR_ACTIVE, (finding,), (), used, obs.attempt_cap)

    findings: list[Finding] = list(obs.findings)
    history: list[Finding] = []
    if obs.lock == "unknown":
        findings.append(_unknown("COORDINATOR_LOCK_UNKNOWN", "the dispatch lock could not be examined"))
    findings.append(
        _info(
            "MULTI_HOST_UNSUPPORTED",
            "only this host's run records are visible; dispatch from more than one host is not supported",
        )
    )
    for revision in obs.newer_revisions:
        findings.append(
            _uncertain(
                "RECORDS_FOR_NEWER_REVISION",
                f"run records exist for revision {revision}, newer than the task's revision {obs.revision}",
            )
        )

    findings.extend(_record_findings(current))
    findings.extend(_pull_findings(current, obs.integration))
    for name, value in (
        ("local branch", current.local),
        ("worktree", current.worktree),
        ("remote branch", current.remote),
    ):
        if value is None:
            findings.append(
                _unknown("FACT_UNKNOWN", f"whether the {name} {current.branch} exists is unknown", current.branch)
            )

    superseded = False
    for older in obs.older:
        findings.extend(_record_findings(older))
        if older.pulls is None:
            findings.append(
                _unknown("PULL_REQUESTS_UNKNOWN", f"pull requests from {older.branch} could not be read", older.branch)
            )
            continue
        for pull in older.pulls:
            if pull.state == "open":
                superseded = True
                findings.append(
                    Finding(
                        "SUPERSEDED_PR_OPEN",
                        "BLOCKING",
                        f"PR #{pull.number} from revision {older.revision} ({older.branch}) is still open",
                        older.branch,
                    )
                )
            elif pull.state == "merged":
                history.append(
                    _info(
                        "OLDER_REVISION_MERGED",
                        f"revision {older.revision} was merged in PR #{pull.number}; history only, not evidence or "
                        f"approval for revision {obs.revision}",
                        older.branch,
                    )
                )
            else:
                history.append(
                    _info("OLDER_REVISION_PR_CLOSED", f"PR #{pull.number} from revision {older.revision} was closed")
                )
        if older.remote or older.local or older.worktree:
            history.append(_info("OLDER_REVISION_BRANCH_PRESENT", f"{older.branch} still exists", older.branch))

    results = {f.result for f in findings}
    pulls = current.pulls or ()
    records = current.records
    has_records = records is not None and bool(records.attempts)
    exhausted = used >= obs.attempt_cap

    if "UNCERTAIN" in results:
        state = State.UNCERTAIN
    elif "UNKNOWN" in results:
        state = State.UNKNOWN
    elif superseded:
        state = State.SUPERSEDED_PR_OPEN
    elif not has_records:
        if pulls or current.remote:
            state = State.UNTRACKED_BRANCH
            detail = ", ".join(f"PR #{p.number} {p.state}" for p in pulls) or "no pull request"
            findings.append(
                Finding(
                    "BRANCH_WITHOUT_RECORD",
                    "BLOCKING",
                    f"{current.branch} exists on GitHub ({detail}) but this host has no run record for it",
                    current.branch,
                )
            )
        elif current.local or current.worktree:
            state = State.UNTRACKED_LOCAL_BRANCH
            findings.append(
                Finding(
                    "LOCAL_BRANCH_WITHOUT_RECORD",
                    "BLOCKING",
                    f"{current.branch} exists locally (branch or worktree) with no run record and no remote branch",
                    current.branch,
                )
            )
        else:
            state = State.NO_RUN
    else:
        assert records is not None and records.last is not None and records.last.outcome is not None
        outcome = records.last.outcome
        if any(p.state == "merged" for p in pulls):
            state = State.PR_MERGED
        elif any(p.state == "open" for p in pulls):
            state = State.PR_OPEN
        elif outcome["status"] == "abandoned":
            state = State.ABANDONED
            if current.remote or current.local or current.worktree:
                findings.append(
                    Finding(
                        "BRANCH_PRESENT_AFTER_ABANDON",
                        "BLOCKING",
                        f"{current.branch} still exists; Archon would reuse it for another attempt",
                        current.branch,
                    )
                )
        elif pulls:
            state = State.PR_CLOSED
        elif outcome["status"] == "exited" and outcome["exitCode"] != 0:
            state = State.RUN_FAILED
        else:
            state = State.NO_PR
            if not current.remote:
                findings.append(
                    _info(
                        "BRANCH_NOT_PUSHED",
                        f"{current.branch} is not on GitHub"
                        + (" but exists locally" if current.local or current.worktree else ""),
                        current.branch,
                    )
                )
        if exhausted and state in (State.ABANDONED, State.RUN_FAILED, State.NO_PR, State.PR_CLOSED):
            findings.append(
                Finding(
                    "ATTEMPTS_EXHAUSTED",
                    "BLOCKING",
                    f"{used} of {obs.attempt_cap} attempts used for revision {obs.revision}",
                )
            )
            state = State.ATTEMPTS_EXHAUSTED
    return Reconciliation(state, tuple(sorted(set(findings))), tuple(sorted(set(history))), used, obs.attempt_cap)


# -- observation ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class RemoteFacts:
    """Remote branches of this task (by revision) and pull requests (by branch). None: unread."""

    branches: frozenset[int] | None  # revisions whose branch exists on GitHub; None: unread
    pulls: dict[str, tuple[Pull, ...] | None]
    findings: tuple[Finding, ...]


def _revision_branch(repo: Repository, task: Task, revision: int) -> str:
    return f"{repo.feature_prefix}{task.id}-r{revision}"


# matching-refs has no pagination or completeness signal; a listing this long is not trusted.
MAX_MATCHING_REFS = 100


def remote_branch_names(api: GitHubApi, repo: Repository, prefix: str) -> set[str]:
    """Names of the branches on GitHub that start with ``prefix``. Read-only.

    Every entry must be a complete ref (``refs/heads/...`` name inside the prefix, an object id). A
    malformed entry, or a listing too long to be known complete, raises ``GitHubError``: an
    incomplete listing is never an empty one.
    """
    refs = api.get(f"repos/{repo.github}/git/matching-refs/heads/{path_of(prefix)}")
    if not isinstance(refs, list):
        raise GitHubError(f"matching refs for {prefix}: expected a list")
    if len(refs) >= MAX_MATCHING_REFS:
        raise GitHubError(f"matching refs for {prefix}: {len(refs)} entries; the listing may be incomplete")
    names = set()
    for index, ref in enumerate(refs):
        name = ref.get("ref") if isinstance(ref, dict) else None
        target = ref.get("object") if isinstance(ref, dict) else None
        sha = target.get("sha") if isinstance(target, dict) else None
        if not isinstance(name, str) or not name.startswith("refs/heads/") or not is_object_id(sha):
            raise GitHubError(f"matching refs for {prefix}: entry {index} is incomplete")
        branch = name[len("refs/heads/") :]
        if not branch.startswith(prefix):
            raise GitHubError(f"matching refs for {prefix}: entry {index} is outside the requested prefix")
        names.add(branch)
    return names


def read_remote(api: GitHubApi, repo: Repository, task: Task) -> RemoteFacts:
    """Read-only GitHub reads: matching branch refs and pull requests for every revision's branch."""
    findings: list[Finding] = []
    prefix = f"{repo.feature_prefix}{task.id}-r"
    exact = re.compile(re.escape(f"refs/heads/{prefix}") + r"([1-9][0-9]{0,8})")
    branches: frozenset[int] | None
    try:
        found = set()
        for name in remote_branch_names(api, repo, prefix):
            match = exact.fullmatch(f"refs/heads/{name}")
            if match:
                found.add(int(match.group(1)))
        # Only the revisions that exist are kept: memory is bounded by the listing, not the revision.
        branches = frozenset(found)
        for revision in sorted(found):
            if revision > task.revision:
                findings.append(
                    _uncertain(
                        "BRANCH_FOR_NEWER_REVISION",
                        f"{_revision_branch(repo, task, revision)} exists on GitHub, newer than revision {task.revision}",
                    )
                )
    except GitHubError as exc:
        branches = None
        findings.append(_unknown("REMOTE_BRANCHES_UNKNOWN", f"branches of {task.id} could not be read: {exc.message}"))

    pulls: dict[str, tuple[Pull, ...] | None] = {}
    first = max(1, task.revision - MAX_HISTORY)
    if first > 1:
        findings.append(
            _unknown("HISTORY_TRUNCATED", f"revisions before {first} were not examined (limit {MAX_HISTORY})")
        )
    for revision in range(first, task.revision + 1):
        branch = _revision_branch(repo, task, revision)
        try:
            entries = branch_pull_requests(api, repo, branch)
            pulls[branch] = tuple(Pull(e["number"], e["state"], e["baseBranch"]) for e in entries)
        except (GitHubError, KeyError, TypeError):
            pulls[branch] = None
    return RemoteFacts(branches, pulls, tuple(findings))


def _fact(state: str) -> bool | None:
    """``present`` -> True, ``absent`` -> False, anything else (``unknown``) -> None."""
    return {PRESENT: True, ABSENT: False}.get(state)


def _local(checkout: CheckoutState | None, branch: str) -> tuple[bool | None, bool | None]:
    if checkout is None:
        return None, None
    return _fact(checkout.branch_state(branch)), _fact(checkout.worktree_state(branch))


def _local_ref_findings(checkout: CheckoutState, branches: list[str]) -> list[Finding]:
    """Unresolved or unknown local state that concerns this task's branches is UNCERTAIN, never a note."""
    findings = []
    for branch in branches:
        why = checkout.local.unresolved.get(branch)
        if why is not None:
            findings.append(_uncertain("LOCAL_REF_UNRESOLVED", f"local ref {branch} {why}", branch))
        for name, why in checkout.remote.unresolved.items():
            if name.partition("/")[2] == branch:
                findings.append(
                    _uncertain("REMOTE_TRACKING_REF_UNRESOLVED", f"remote-tracking ref {name} {why}", branch)
                )
    targets = {f"'refs/heads/{b}'" for b in branches}
    for name, why in checkout.local.unresolved.items():
        if any(target in why for target in targets):
            findings.append(_uncertain("SYMBOLIC_REF_TO_TASK_BRANCH", f"local ref {name} {why}", name))
    return findings


def read_local(checkout_path: Path, repo: Repository) -> tuple[CheckoutState | None, list[Finding]]:
    try:
        state = read_checkout(checkout_path)
    except GitStateError as exc:
        return None, [_unknown(exc.code, exc.message)]
    slug = github_slug(state.origin_url)
    if slug is None:
        return None, [_unknown("CHECKOUT_REMOTE_UNKNOWN", "the checkout's origin is not a recognizable GitHub URL")]
    if slug != repo.github.lower():
        return None, [
            _uncertain(
                "CHECKOUT_REPOSITORY_MISMATCH", f"the checkout's origin is {slug}, the manifest says {repo.github}"
            )
        ]
    # Notes only; anything that concerns the task's own branches is raised again by observe().
    findings = [_info("CHECKOUT_NOTE", problem) for problem in state.problems]
    return state, findings


def held(project: Project, task: Task) -> Observations:
    """Observations for a refused run: another coordinator holds the lock, so nothing else is read."""
    repo = project.manifest.repository(task.repository)
    assert repo is not None
    empty = RevisionFacts(task.revision, task_branch(repo, task), None, None, None, None, None)
    return Observations(
        project.manifest.project_id,
        task.id,
        task.revision,
        repo.integration_branch,
        project.manifest.dispatch.max_attempts,
        "held",
        empty,
        (),
        (),
        (),
    )


def observe(
    project: Project,
    task: Task,
    checkout_path: Path | None,
    state_root: Path,
    api: GitHubApi | None,
    checkout: CheckoutState | None = None,
    checkout_findings: list[Finding] | None = None,
) -> Observations:
    """Gather every fact for ``task``. ``api`` None means offline: remote facts are UNKNOWN."""
    repo = project.manifest.repository(task.repository)
    assert repo is not None
    findings: list[Finding] = []
    lock = probe_lock(state_root, project.manifest.project_id)
    if lock == "held":
        return held(project, task)

    records: TaskRecords = read_task_records(state_root, project.manifest.project_id, task.id)
    findings.extend(_uncertain(p.code, p.message) for p in records.problems)
    if checkout is None and checkout_path is not None:
        checkout, checkout_findings = read_local(checkout_path, repo)
    findings.extend(checkout_findings or [])
    if checkout_path is None and checkout is None:
        findings.append(_unknown("CHECKOUT_NOT_GIVEN", "no local checkout was given; local branches are unknown"))

    if api is None:
        remote = RemoteFacts(None, {}, (_unknown("REMOTE_NOT_READ", "offline: GitHub branches and PRs were not read"),))
    else:
        remote = read_remote(api, repo, task)
    findings.extend(remote.findings)

    def facts(revision: int) -> RevisionFacts:
        branch = _revision_branch(repo, task, revision)
        local, worktree = _local(checkout, branch)
        return RevisionFacts(
            revision=revision,
            branch=branch,
            records=records.revision(revision) if records.readable else None,
            local=local,
            worktree=worktree,
            remote=revision in remote.branches if remote.branches is not None else None,
            pulls=remote.pulls.get(branch) if api is not None else None,
        )

    if not is_branch(_revision_branch(repo, task, task.revision)):
        findings.append(_uncertain("FEATURE_BRANCH_UNSAFE", "the task's feature branch is not a valid branch name"))
    older = tuple(facts(r) for r in range(max(1, task.revision - MAX_HISTORY), task.revision))
    newer = tuple(r for r in records.revisions if r > task.revision)
    if checkout is not None:
        own = [_revision_branch(repo, task, r) for r in range(max(1, task.revision - MAX_HISTORY), task.revision + 1)]
        findings.extend(_local_ref_findings(checkout, own))
        prefix = f"{repo.feature_prefix}{task.id}-r"
        for name in checkout.local.names():
            suffix = name[len(prefix) :] if name.startswith(prefix) else ""
            if suffix.isdigit() and not suffix.startswith("0") and int(suffix) > task.revision:
                findings.append(
                    _uncertain(
                        "BRANCH_FOR_NEWER_REVISION", f"local branch {name} is newer than revision {task.revision}"
                    )
                )
    return Observations(
        project=project.manifest.project_id,
        task=task.id,
        revision=task.revision,
        integration=repo.integration_branch,
        attempt_cap=project.manifest.dispatch.max_attempts,
        lock=lock,
        current=facts(task.revision),
        older=older,
        newer_revisions=newer,
        findings=tuple(findings),
    )


def reconcile_document(task: Task, repo: Repository, result: Reconciliation, obs: Observations) -> dict[str, Any]:
    def revision(facts: RevisionFacts) -> dict[str, Any]:
        return {
            "attempts": [a.to_dict() for a in facts.records.attempts] if facts.records is not None else None,
            "branch": facts.branch,
            "localBranch": facts.local,
            "pullRequests": [{"base": p.base, "number": p.number, "state": p.state} for p in facts.pulls]
            if facts.pulls is not None
            else None,
            "remoteBranch": facts.remote,
            "revision": facts.revision,
            "worktree": facts.worktree,
        }

    return {
        **result.to_dict(),
        "lock": obs.lock,
        "project": obs.project,
        "repository": repo.id,
        "revisions": [revision(f) for f in (*obs.older, obs.current)] if obs.lock != "held" else [],
        "schemaVersion": RECONCILE_SCHEMA_VERSION,
        "task": task.id,
        "taskRevision": task.revision,
    }
