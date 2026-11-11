"""Pure dependency evaluation: (project, evidence snapshot) -> plan.

Every check yields PASS, FAIL or UNKNOWN. UNKNOWN never lets anything proceed. Results are
deterministic: no clocks, no randomness, all collections sorted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from factory.evidence import CommitFacts, RepositoryFacts, Snapshot
from factory.model import LEVELS, Project, Repository, RequiredCheck, Task, level_index
from factory.semver import VersionError, parse_version, satisfies

PLAN_SCHEMA_VERSION = "factory.plan/v1"
INTEGRATION_EVENT = "push"
FAILED_CONCLUSIONS = frozenset({"failure", "cancelled", "timed_out", "action_required", "startup_failure"})


class Result(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"


class State(StrEnum):
    STALE = "STALE"
    COMPLETE = "COMPLETE"
    MERGED = "MERGED"
    BLOCKED = "BLOCKED"
    UNKNOWN = "UNKNOWN"
    IN_REVIEW = "IN_REVIEW"
    READY = "READY"


@dataclass(frozen=True)
class Reason:
    code: str
    result: Result
    message: str
    ref: str = ""
    causes: tuple[Reason, ...] = ()

    def sort_key(self) -> tuple[str, str, str]:
        return (self.code, self.ref, self.message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "causes": [c.to_dict() for c in self.causes],
            "code": self.code,
            "message": self.message,
            "ref": self.ref,
            "result": self.result.value,
        }


def _fail(code: str, message: str, ref: str = "", causes: list[Reason] | None = None) -> Reason:
    return Reason(code, Result.FAIL, message, ref, _sorted(causes or []))


def _unknown(code: str, message: str, ref: str = "", causes: list[Reason] | None = None) -> Reason:
    return Reason(code, Result.UNKNOWN, message, ref, _sorted(causes or []))


def _sorted(reasons: list[Reason]) -> tuple[Reason, ...]:
    unique = {r.sort_key(): r for r in reasons}
    return tuple(unique[k] for k in sorted(unique))


def combine(results: list[Result]) -> Result:
    if Result.FAIL in results:
        return Result.FAIL
    if Result.UNKNOWN in results:
        return Result.UNKNOWN
    return Result.PASS


def _wrap(code: str, message: str, ref: str, causes: list[Reason]) -> list[Reason]:
    """Group ``causes`` under one parent reason that carries their combined result."""
    if not causes:
        return []
    return [Reason(code, combine([c.result for c in causes]), message, ref, _sorted(causes))]


@dataclass(frozen=True)
class Outcome:
    result: Result
    reasons: tuple[Reason, ...]

    @staticmethod
    def of(reasons: list[Reason]) -> Outcome:
        ordered = _sorted(reasons)
        return Outcome(combine([r.result for r in ordered]), ordered)

    def to_dict(self) -> dict[str, Any]:
        return {"reasons": [r.to_dict() for r in self.reasons], "result": self.result.value}


@dataclass(frozen=True)
class LevelEvaluation:
    """``own`` holds reasons introduced at this level; ``result`` is cumulative over lower levels."""

    result: Result
    own: tuple[Reason, ...]


@dataclass(frozen=True)
class EdgeEvaluation:
    task: str
    gate: str
    requires: str
    dependency_level: str | None
    outcome: Outcome

    def to_dict(self) -> dict[str, Any]:
        return {
            "dependencyLevel": self.dependency_level,
            "gate": self.gate,
            "reasons": [r.to_dict() for r in self.outcome.reasons],
            "requires": self.requires,
            "result": self.outcome.result.value,
            "task": self.task,
        }


@dataclass(frozen=True)
class TaskPlan:
    task: Task
    branch: str
    state: State
    levels: dict[str, LevelEvaluation]
    gates: dict[str, Outcome]
    architecture: Outcome
    stale: tuple[Reason, ...]
    dependencies: tuple[EdgeEvaluation, ...]

    @property
    def achieved_level(self) -> str | None:
        achieved = None
        for level in LEVELS:
            if self.levels[level].result is not Result.PASS:
                break
            achieved = level
        return achieved

    def to_dict(self) -> dict[str, Any]:
        task = self.task
        return {
            "achievedLevel": self.achieved_level,
            "architecture": {
                "profile": task.profile,
                "profileRevision": task.profile_revision,
                "required": task.requires_profile,
                **self.architecture.to_dict(),
            },
            "branch": self.branch,
            "dependencies": [d.to_dict() for d in self.dependencies],
            "gates": {name: outcome.to_dict() for name, outcome in self.gates.items()},
            "id": task.id,
            "kind": task.kind,
            "levels": {
                level: {"reasons": [r.to_dict() for r in ev.own], "result": ev.result.value}
                for level, ev in self.levels.items()
            },
            "repository": task.repository,
            "revision": task.revision,
            "stale": [r.to_dict() for r in self.stale],
            "state": self.state.value,
        }


@dataclass(frozen=True)
class Plan:
    project: Project
    snapshot: Snapshot
    tasks: tuple[TaskPlan, ...] = field(default_factory=tuple)

    def summary(self) -> dict[str, int]:
        counts = {state.value: 0 for state in State}
        for plan in self.tasks:
            counts[plan.state.value] += 1
        return dict(sorted(counts.items()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifacts": dict(sorted(self.project.digests.items())),
            "evidence": {
                "authenticated": self.snapshot.authenticated,
                "observedAt": self.snapshot.observed_at,
                "provenance": self.snapshot.provenance,
            },
            "project": self.project.manifest.project_id,
            "schemaVersion": PLAN_SCHEMA_VERSION,
            "summary": self.summary(),
            "tasks": [t.to_dict() for t in self.tasks],
        }


def task_branch(repo: Repository, task: Task) -> str:
    return f"{repo.feature_prefix}{task.id}-r{task.revision}"


@dataclass(frozen=True)
class _MergeFact:
    number: int
    sha: str


class _Evaluator:
    def __init__(self, project: Project, snapshot: Snapshot) -> None:
        self.project = project
        self.snapshot = snapshot
        self.manifest = project.manifest
        self._levels: dict[str, tuple[dict[str, LevelEvaluation], _MergeFact | None]] = {}

    # -- approvals and staleness -------------------------------------------------

    def _approval(self, path: str, what: str, changed_code: str) -> tuple[list[Reason], list[Reason]]:
        """Return (blocking reasons, staleness reasons) for one approvable artifact."""
        current = self.project.digests.get(path)
        entry = self.snapshot.approvals.get(path)
        if entry is None:
            return [_unknown("APPROVAL_EVIDENCE_MISSING", f"no approval evidence for {what} {path}", path)], []
        if entry.sha256 != current:
            return [], [_fail(changed_code, f"{what} {path} changed after the approved revision", path)]
        if not entry.approved:
            detail = f" ({entry.evidence})" if entry.evidence else ""
            return [_fail("NOT_APPROVED", f"{what} {path} is not approved{detail}", path)], []
        if entry.merged_by not in self.manifest.approvers:
            merged_by = entry.merged_by or "nobody"
            return [
                _fail(
                    "APPROVER_NOT_AUTHORIZED", f"{what} {path} was merged by {merged_by}, who is not an approver", path
                )
            ], []
        # A GitHub login proves which credential merged, not which person. A collected snapshot
        # stays unverifiable even if the marker was edited out.
        if entry.human_authorization is not None or self.snapshot.provenance == "github":
            return [
                _unknown(
                    "HUMAN_AUTHORIZATION_UNVERIFIABLE",
                    f"{what} {path} was merged on GitHub by {entry.merged_by}, but that login does not prove a human "
                    "approved it while agents can use the approver's credentials",
                    path,
                )
            ], []
        return [], []

    def _task_own(self, task: Task) -> tuple[list[Reason], list[Reason]]:
        blocking, stale = self._approval(task.file, "task", "TASK_CHANGED_SINCE_APPROVAL")
        if self.project.digests.get(task.spec_path) != task.spec_sha256:
            stale.append(
                _fail(
                    "SPEC_CHANGED",
                    f"specification {task.spec_path} no longer matches the hash pinned by the task",
                    task.spec_path,
                )
            )
        return blocking, stale

    def _architecture(self, task: Task) -> tuple[list[Reason], list[Reason]]:
        if not task.requires_profile or task.profile is None:
            return [], []
        profile = self.project.profiles[task.profile]
        blocking: list[Reason] = []
        stale: list[Reason] = []
        if task.profile_revision != profile.revision:
            stale.append(
                _fail(
                    "PROFILE_REVISION_MISMATCH",
                    f"task pins profile {profile.id} revision {task.profile_revision}, profile is at revision {profile.revision}",
                    profile.id,
                )
            )
        approval, changed = self._approval(profile.file, "profile", "PROFILE_CHANGED_SINCE_APPROVAL")
        blocking += _wrap(
            "PROFILE_NOT_APPROVED", f"architecture profile {profile.id} is not approved", profile.id, approval
        )
        stale += changed
        for pin in profile.adrs:
            adr = self.project.adrs[pin.id]
            if adr.revision != pin.revision:
                stale.append(
                    _fail(
                        "ADR_REVISION_MISMATCH",
                        f"profile {profile.id} pins {adr.id} revision {pin.revision}, ADR is at revision {adr.revision}",
                        adr.id,
                    )
                )
            if adr.status != "accepted":
                blocking.append(_fail("ADR_NOT_ACCEPTED", f"{adr.id} has status '{adr.status}'", adr.id))
            approval, changed = self._approval(adr.file, "ADR", "ADR_CHANGED_SINCE_APPROVAL")
            blocking += _wrap("ADR_NOT_APPROVED", f"{adr.id} is not approved", adr.id, approval)
            stale += changed
        return blocking, stale

    # -- evidence rules ------------------------------------------------------------

    def _check(self, repo: Repository, commit: CommitFacts, check: RequiredCheck, ref: str) -> list[Reason]:
        label = f"check '{check.name}'"
        if commit.check_runs is None:
            return [_unknown("CHECK_EVIDENCE_MISSING", f"no check-run evidence for {ref}", ref)]
        runs = [r for r in commit.check_runs if r.name == check.name]
        qualifying = [
            r
            for r in runs
            if r.app == check.app
            and r.workflow_path == check.workflow_path
            and r.event == INTEGRATION_EVENT
            and r.head_branch == repo.integration_branch
        ]
        if not qualifying:
            if any(r.app != check.app or r.workflow_path != check.workflow_path for r in runs):
                return [
                    _fail(
                        "CHECK_PROVENANCE_MISMATCH",
                        f"{label} on {ref} was reported by an unexpected app or workflow "
                        f"(expected app '{check.app}', workflow '{check.workflow_path}')",
                        ref,
                    )
                ]
            return [
                _unknown(
                    "CHECK_MISSING",
                    f"no {INTEGRATION_EVENT} run of {label} on branch '{repo.integration_branch}' for {ref}",
                    ref,
                )
            ]
        latest_attempt = max(r.attempt for r in qualifying)
        latest = [r for r in qualifying if r.attempt == latest_attempt]
        if len({(r.status, r.conclusion) for r in latest}) > 1:
            return [_unknown("AMBIGUOUS_CHECK_RUN", f"conflicting runs of {label} on {ref}", ref)]
        run = latest[0]
        if run.status != "completed":
            return [_unknown("CHECK_IN_PROGRESS", f"{label} on {ref} is {run.status}", ref)]
        if run.conclusion == "success":
            return []
        if run.conclusion in FAILED_CONCLUSIONS:
            return [_fail("CHECK_FAILED", f"{label} on {ref} concluded '{run.conclusion}'", ref)]
        return [_unknown("CHECK_INCONCLUSIVE", f"{label} on {ref} concluded '{run.conclusion}'", ref)]

    def _validated_commit(
        self, repo: Repository, facts: RepositoryFacts, sha: str, expected_pr: int | None
    ) -> list[Reason]:
        ref = f"{repo.id}@{sha}"
        commit = facts.commits.get(sha)
        if commit is None:
            return [_unknown("COMMIT_EVIDENCE_MISSING", f"no evidence for commit {ref}", ref)]
        reasons: list[Reason] = []
        if commit.reachable is None:
            reasons.append(
                _unknown("REACHABILITY_UNKNOWN", f"unknown whether {ref} is reachable from the integration head", ref)
            )
        elif not commit.reachable:
            reasons.append(_fail("COMMIT_NOT_REACHABLE", f"{ref} is not reachable from the integration head", ref))
        mapped = commit.merged_pull_requests
        if mapped is None:
            reasons.append(_unknown("PR_MAPPING_UNKNOWN", f"unknown which pull request produced {ref}", ref))
        elif len(mapped) == 0:
            reasons.append(_fail("COMMIT_NOT_PR_MERGE", f"{ref} is not the merge commit of any pull request", ref))
        elif len(set(mapped)) > 1:
            reasons.append(_unknown("AMBIGUOUS_PR_MAPPING", f"{ref} maps to several pull requests", ref))
        elif expected_pr is not None and mapped[0] != expected_pr:
            reasons.append(
                _fail(
                    "PR_MAPPING_MISMATCH", f"{ref} is the merge commit of PR #{mapped[0]}, not PR #{expected_pr}", ref
                )
            )
        if not repo.required_checks:
            reasons.append(
                _unknown("NO_REQUIRED_CHECKS", f"repository {repo.id} declares no required integration checks", ref)
            )
        for check in repo.required_checks:
            reasons += self._check(repo, commit, check, ref)
        return reasons

    def _output_version(
        self, repo: Repository, facts: RepositoryFacts | None, output_id: str
    ) -> tuple[list[Reason], str | None]:
        if facts is None or facts.integration_head is None:
            return [_unknown("OUTPUT_EVIDENCE_MISSING", f"no integration head known for {repo.id}", output_id)], None
        ref = f"{repo.id}@{facts.integration_head}"
        commit = facts.commits.get(facts.integration_head)
        fact = commit.outputs.get(output_id) if commit is not None and commit.outputs is not None else None
        if fact is None:
            return [
                _unknown("OUTPUT_EVIDENCE_MISSING", f"no evidence for output '{output_id}' at {ref}", output_id)
            ], None
        if not fact.present:
            return [_fail("OUTPUT_MISSING", f"output '{output_id}' is absent at {ref}", output_id)], None
        if fact.version is None:
            return [
                _unknown("OUTPUT_VERSION_UNKNOWN", f"output '{output_id}' at {ref} has no recorded version", output_id)
            ], None
        try:
            parse_version(fact.version)
        except VersionError as exc:
            return [_fail("OUTPUT_VERSION_INVALID", f"output '{output_id}' at {ref}: {exc}", output_id)], None
        return [], fact.version

    # -- assurance levels ----------------------------------------------------------

    def levels(self, task: Task) -> tuple[dict[str, LevelEvaluation], _MergeFact | None]:
        if task.id in self._levels:
            return self._levels[task.id]
        repo = self.manifest.repository(task.repository)
        assert repo is not None  # guaranteed by validation
        facts = self.snapshot.repositories.get(repo.id)
        own: dict[str, list[Reason]] = {level: [] for level in LEVELS}

        blocking, stale = self._task_own(task)
        own["approved"] = blocking + _wrap("TASK_STALE", f"task {task.id} is stale", task.id, stale)

        branch = task_branch(repo, task)
        merge: _MergeFact | None = None
        if facts is None or branch not in facts.branches:
            own["merged"].append(
                _unknown("PR_EVIDENCE_MISSING", f"no pull-request evidence for branch {branch}", branch)
            )
        else:
            pulls = facts.branches[branch]
            merged = [p for p in pulls if p.state == "merged"]
            into = [p for p in merged if p.base_branch == repo.integration_branch]
            if len(into) > 1:
                own["merged"].append(
                    _unknown("AMBIGUOUS_PULL_REQUEST", f"several merged pull requests from {branch}", branch)
                )
            elif len(into) == 1:
                if into[0].merge_commit_sha is None:
                    own["merged"].append(
                        _unknown("MERGE_COMMIT_UNKNOWN", f"PR #{into[0].number} has no recorded merge commit", branch)
                    )
                else:
                    merge = _MergeFact(into[0].number, into[0].merge_commit_sha)
            elif merged:
                bases = ", ".join(sorted({p.base_branch for p in merged}))
                own["merged"].append(
                    _fail(
                        "PR_MERGED_INTO_WRONG_BASE",
                        f"{branch} was merged into {bases}, not {repo.integration_branch}",
                        branch,
                    )
                )
            else:
                open_numbers = sorted(p.number for p in pulls if p.state == "open")
                detail = f"PR #{open_numbers[0]} is open" if open_numbers else "no merged pull request"
                own["merged"].append(_fail("NOT_MERGED", f"{branch} is not merged: {detail}", branch))

        if merge is not None and facts is not None:
            own["validated"] = self._validated_commit(repo, facts, merge.sha, merge.number)

        if facts is None or facts.integration_head is None:
            own["integration-ready"].append(
                _unknown("INTEGRATION_HEAD_UNKNOWN", f"integration head of {repo.id} is unknown", repo.id)
            )
        else:
            head_reasons = self._validated_commit(repo, facts, facts.integration_head, None)
            own["integration-ready"] += _wrap(
                "HEAD_NOT_VALIDATED",
                f"integration head {repo.id}@{facts.integration_head} is not validated",
                f"{repo.id}@{facts.integration_head}",
                head_reasons,
            )
        for output_id in task.provides:
            own["integration-ready"] += self._output_version(repo, facts, output_id)[0]

        own["environment-verified"].append(
            _unknown(
                "ENVIRONMENT_EVIDENCE_UNSUPPORTED",
                "environment deployment evidence is not supported by this Factory Core version",
                task.id,
            )
        )

        evaluations: dict[str, LevelEvaluation] = {}
        previous = Result.PASS
        for level in LEVELS:
            reasons = _sorted(own[level])
            previous = combine([previous, *(r.result for r in reasons)])
            evaluations[level] = LevelEvaluation(previous, reasons)
        self._levels[task.id] = (evaluations, merge)
        return self._levels[task.id]

    # -- edges and gates -------------------------------------------------------------

    def edge(self, task: Task, edge_index: int) -> EdgeEvaluation:
        edge = task.depends_on[edge_index]
        dependency = self.project.tasks[edge.task]
        levels, _ = self.levels(dependency)
        required = level_index(edge.requires)
        upto = LEVELS[: required + 1]
        result = combine([levels[level].result for level in upto])
        achieved = None
        for level in LEVELS:
            if levels[level].result is not Result.PASS:
                break
            achieved = level

        reasons: list[Reason] = []
        if result is not Result.PASS:
            causes = [reason for level in upto for reason in levels[level].own]
            reasons.append(
                Reason(
                    "DEP_LEVEL_UNMET",
                    result,
                    f"{dependency.id} has not reached '{edge.requires}' (achieved: {achieved or 'none'})",
                    dependency.id,
                    _sorted(causes),
                )
            )
            if result is Result.UNKNOWN:
                reasons.append(
                    _unknown(
                        "EVIDENCE_UNKNOWN",
                        f"evidence needed to evaluate {dependency.id} is missing or inconclusive",
                        dependency.id,
                    )
                )

        if required >= level_index("integration-ready"):
            dep_repo = self.manifest.repository(dependency.repository)
            assert dep_repo is not None
            facts = self.snapshot.repositories.get(dep_repo.id)
            for consumption in task.consumes:
                if consumption.output not in dependency.provides:
                    continue
                _, version = self._output_version(dep_repo, facts, consumption.output)
                if version is not None and not satisfies(version, consumption.version):
                    reasons.append(
                        _fail(
                            "CONTRACT_INCOMPATIBLE",
                            f"output '{consumption.output}' is at version {version}, task requires '{consumption.version}'",
                            consumption.output,
                        )
                    )
        return EdgeEvaluation(edge.task, edge.gate, edge.requires, achieved, Outcome.of(reasons))

    def plan_task(self, task: Task) -> TaskPlan:
        repo = self.manifest.repository(task.repository)
        assert repo is not None
        levels, _ = self.levels(task)
        approval, own_stale = self._task_own(task)
        arch_blocking, arch_stale = self._architecture(task)
        stale = _sorted(own_stale + arch_stale)
        edges = tuple(self.edge(task, index) for index in range(len(task.depends_on)))

        def edge_reasons(gate: str) -> list[Reason]:
            return [r for e in edges if e.gate == gate for r in e.outcome.reasons]

        own_preconditions = _wrap("TASK_NOT_APPROVED", f"task {task.id} is not approved", task.id, approval)
        preconditions = own_preconditions + arch_blocking + list(stale)
        not_validated = [r for level in LEVELS[: level_index("validated") + 1] for r in levels[level].own]
        gates = {
            "start": Outcome.of(preconditions + edge_reasons("start")),
            "openPullRequest": Outcome.of(preconditions),
            "merge": Outcome.of(preconditions + edge_reasons("start") + edge_reasons("merge")),
            "complete": Outcome.of(
                _wrap("TASK_NOT_VALIDATED", f"task {task.id} has not reached 'validated'", task.id, not_validated)
                + list(stale)
                + edge_reasons("complete")
            ),
        }

        facts = self.snapshot.repositories.get(repo.id)
        branch = task_branch(repo, task)
        has_open_pr = facts is not None and any(
            p.state == "open" and p.base_branch == repo.integration_branch for p in facts.branches.get(branch, ())
        )
        start = gates["start"].result
        if stale:
            state = State.STALE
        elif gates["complete"].result is Result.PASS:
            state = State.COMPLETE
        elif levels["merged"].result is Result.PASS:
            state = State.MERGED
        elif start is Result.FAIL:
            state = State.BLOCKED
        elif start is Result.UNKNOWN:
            state = State.UNKNOWN
        elif has_open_pr:
            state = State.IN_REVIEW
        else:
            state = State.READY

        return TaskPlan(
            task=task,
            branch=branch,
            state=state,
            levels=levels,
            gates=gates,
            architecture=Outcome.of(arch_blocking + list(arch_stale)),
            stale=stale,
            dependencies=edges,
        )


def evaluate(project: Project, snapshot: Snapshot) -> Plan:
    """Evaluate every task. The project must have passed validation (acyclic, references resolved)."""
    evaluator = _Evaluator(project, snapshot)
    plans = tuple(evaluator.plan_task(project.tasks[task_id]) for task_id in sorted(project.tasks))
    return Plan(project, snapshot, plans)
