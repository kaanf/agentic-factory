"""Builds an evidence snapshot from live GitHub data. Read-only.

The collector reports facts; it does not judge them. Judging (event, branch, app, workflow path,
attempts, PR mapping) stays in ``planning``. One rule governs collection: a fact that cannot be
collected completely is left out of the snapshot, so the planner treats it as UNKNOWN. A partial
list of check runs could hide a failed rerun, so it is never written.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from factory import __version__
from factory.approvals import collect_approvals, credential_isolation
from factory.documents import DocumentError, parse_yaml
from factory.github import GitHubApi, GitHubError, path_of, segment
from factory.model import Output, Project, Repository, Task
from factory.planning import task_branch
from factory.schemas.validation import validate_document

ACTIONS_APP = "github-actions"
CHECK_STATUSES = frozenset({"queued", "in_progress", "completed"})
CHECK_CONCLUSIONS = frozenset(
    {
        "success",
        "failure",
        "neutral",
        "cancelled",
        "skipped",
        "timed_out",
        "action_required",
        "stale",
        "startup_failure",
    }
)
APPROVALS_NOTE = (
    "collected read-only from the GitHub REST API by the GitHub CLI; the planner cannot verify this file's integrity, "
    "and approvals record GitHub merge provenance only: human authorization is unverifiable, so no approval passes"
)


@dataclass(frozen=True, order=True)
class CollectionError:
    repository: str
    fact: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"fact": self.fact, "message": self.message, "repository": self.repository}

    def render(self) -> str:
        return f"{self.repository}: {self.fact}: {self.message}"


@dataclass(frozen=True)
class CollectionResult:
    snapshot: dict[str, Any]
    errors: list[CollectionError]


class _IncompleteError(GitHubError):
    """The response was readable but did not account for every item."""


def _json_pointer(document: Any, pointer: str) -> Any:
    current = document
    for raw in pointer.split("/")[1:]:
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict) and token in current:
            current = current[token]
        elif isinstance(current, list) and token.isdigit() and int(token) < len(current):
            current = current[int(token)]
        else:
            return None
    return current


def read_integration_head(api: GitHubApi, repo: Repository) -> str:
    """The commit ``refs/heads/<integration branch>`` points to. Raises ``GitHubError``."""
    try:
        ref = api.get(f"repos/{repo.github}/git/ref/heads/{path_of(repo.integration_branch)}")
        target = ref["object"]
        if target["type"] != "commit":
            raise GitHubError(f"refs/heads/{repo.integration_branch} does not point to a commit")
        sha: str = target["sha"]
        return sha
    except (KeyError, TypeError) as exc:
        raise GitHubError("unexpected git ref response") from exc


def branch_pull_requests(api: GitHubApi, repo: Repository, branch: str) -> list[dict[str, Any]]:
    """Every pull request whose head is ``branch`` in ``repo`` itself (not a fork), by number.

    A record is skipped only when it is confirmed to be about something else: a head ref naming a
    different branch, or a head repository naming a different repository. A record whose identity
    is missing or malformed (no head ref, a null head repository as for a deleted fork, no number,
    state or base) makes the whole listing incomplete, because it might be this branch's PR.

    Raises ``GitHubError`` when the list cannot be read completely or a record is incomplete.
    """
    owner = repo.github.split("/")[0]
    pulls = api.get_list(f"repos/{repo.github}/pulls", state="all", head=f"{owner}:{branch}")
    entries = []
    for index, pr in enumerate(pulls):
        if not isinstance(pr, dict):
            raise _IncompleteError(f"pull request {index} for {branch} is not an object")
        number = pr.get("number")
        if not isinstance(number, int) or isinstance(number, bool) or number < 1:
            raise _IncompleteError(f"pull request {index} for {branch} has no valid number")
        head = pr.get("head")
        if not isinstance(head, dict) or not isinstance(head.get("ref"), str) or not head["ref"]:
            raise _IncompleteError(f"PR #{number} has no head ref, so it may be from {branch}")
        if head["ref"] != branch:
            continue  # confirmed: another branch
        head_repo = head.get("repo")
        full_name = head_repo.get("full_name") if isinstance(head_repo, dict) else None
        if not isinstance(full_name, str) or not full_name:
            raise _IncompleteError(
                f"PR #{number} from {branch} has no head repository identity (deleted fork or incomplete record)"
            )
        if full_name.lower() != repo.github.lower():
            continue  # confirmed: the same branch name in a fork, not this task's PR
        base = pr.get("base")
        if not isinstance(base, dict) or not isinstance(base.get("ref"), str) or not base["ref"]:
            raise _IncompleteError(f"PR #{number} from {branch} has no base ref")
        if pr.get("state") not in ("open", "closed") or "merged_at" not in pr:
            raise _IncompleteError(f"PR #{number} from {branch} has no valid state")
        merged_at = pr["merged_at"]
        if merged_at is not None and (not isinstance(merged_at, str) or pr["state"] != "closed"):
            raise _IncompleteError(f"PR #{number} from {branch} has a contradictory merge state")
        merged = merged_at is not None
        entries.append(
            {
                "baseBranch": base["ref"],
                # Unmerged PRs expose a test-merge SHA here; it is not a merge commit.
                "mergeCommitSha": pr.get("merge_commit_sha") if merged else None,
                "number": number,
                "state": "merged" if merged else pr["state"],
            }
        )
    return sorted(entries, key=lambda e: e["number"])


class _RepositoryCollector:
    def __init__(self, api: GitHubApi, repo: Repository, tasks: list[Task], errors: list[CollectionError]) -> None:
        self.api = api
        self.repo = repo
        self.tasks = tasks
        self.errors = errors
        self.base = f"repos/{repo.github}"

    def _error(self, fact: str, exc: GitHubError) -> None:
        self.errors.append(CollectionError(self.repo.id, fact, exc.message))

    def _head(self) -> str | None:
        try:
            return read_integration_head(self.api, self.repo)
        except GitHubError as exc:
            self._error("integrationHead", exc)
        return None

    def _pull_requests(self, branch: str) -> list[dict[str, Any]] | None:
        try:
            return branch_pull_requests(self.api, self.repo, branch)
        except GitHubError as exc:
            self._error(f"branches.{branch}", exc)
            return None

    def _reachable(self, sha: str, head: str) -> bool:
        comparison = self.api.get(f"{self.base}/compare/{segment(sha)}...{segment(head)}")
        status = comparison.get("status")
        if status in ("ahead", "identical"):
            return True
        if status in ("behind", "diverged"):
            return False
        raise GitHubError(f"unexpected compare status {status!r}")

    def _merged_pull_requests(self, sha: str) -> list[int]:
        pulls = self.api.get_list(f"{self.base}/commits/{segment(sha)}/pulls")
        return sorted(pr["number"] for pr in pulls if pr.get("merged_at") and pr.get("merge_commit_sha") == sha)

    def _check_runs(self, sha: str) -> list[dict[str, Any]]:
        runs = [
            r
            for r in self.api.get_list(f"{self.base}/commits/{segment(sha)}/check-runs", key="check_runs", filter="all")
            if r.get("head_sha") == sha
        ]
        workflow_runs: dict[int, dict[str, Any]] = {}
        attempts: dict[int, int] = {}
        actions_runs = [r for r in runs if (r.get("app") or {}).get("slug") == ACTIONS_APP]
        if actions_runs:
            for run in self.api.get_list(f"{self.base}/actions/runs", key="workflow_runs", head_sha=sha):
                if run.get("head_sha") == sha:
                    workflow_runs[run["check_suite_id"]] = run
            for suite in sorted({r["check_suite"]["id"] for r in actions_runs}):
                if suite not in workflow_runs:
                    raise _IncompleteError(f"no workflow run found for check suite {suite}")
                run_id = workflow_runs[suite]["id"]
                for job in self.api.get_list(f"{self.base}/actions/runs/{run_id}/jobs", key="jobs", filter="all"):
                    check_id = int(str(job.get("check_run_url") or job["id"]).rsplit("/", 1)[-1])
                    attempts[check_id] = job["run_attempt"]

        collected = []
        for run in runs:
            app = (run.get("app") or {}).get("slug") or ""
            if app == ACTIONS_APP:
                if run["id"] not in attempts:
                    raise _IncompleteError(f"no attempt number found for check run {run['id']}")
                workflow = workflow_runs[run["check_suite"]["id"]]
                event, branch, path = workflow["event"], workflow.get("head_branch") or "", workflow["path"]
                attempt = attempts[run["id"]]
            else:
                # Not from Actions: no workflow provenance exists, so it can never match a required check.
                event, branch, path, attempt = "", "", "", 1
            status = run["status"] if run["status"] in CHECK_STATUSES else "in_progress"
            conclusion = run.get("conclusion") if run.get("conclusion") in CHECK_CONCLUSIONS else None
            collected.append(
                {
                    "app": app,
                    "attempt": attempt,
                    "conclusion": conclusion if status == "completed" else None,
                    "event": event,
                    "headBranch": branch,
                    "name": run["name"],
                    "status": status,
                    "workflowPath": path,
                }
            )
        return sorted(collected, key=lambda c: tuple(str(c[k]) for k in sorted(c)))

    def _output(self, sha: str, output: Output) -> dict[str, Any]:
        try:
            body = self.api.get(f"{self.base}/contents/{path_of(output.path)}", ref=sha)
        except GitHubError as exc:
            if exc.status == 404:
                return {"present": False, "version": None}
            raise
        if not isinstance(body, dict) or body.get("type") != "file" or body.get("encoding") != "base64":
            raise _IncompleteError(f"{output.path} is not a regular file readable through the contents API")
        try:
            text = base64.b64decode(body["content"]).decode("utf-8")
            value = _json_pointer(parse_yaml(text), output.version_pointer)
        except (binascii.Error, UnicodeDecodeError, DocumentError):
            value = None
        return {"present": True, "version": value if isinstance(value, str) else None}

    def _commit(self, sha: str, head: str | None, is_head: bool) -> dict[str, Any]:
        facts: dict[str, Any] = {}
        collectors: list[tuple[str, Callable[[], Any]]] = [
            ("mergedPullRequests", lambda: self._merged_pull_requests(sha)),
            ("checkRuns", lambda: self._check_runs(sha)),
        ]
        if head is not None:
            collectors.append(("reachableFromIntegrationHead", lambda: self._reachable(sha, head)))
        for name, collect in collectors:
            try:
                facts[name] = collect()
            except GitHubError as exc:
                self._error(f"commits.{sha}.{name}", exc)
            except (KeyError, TypeError, ValueError) as exc:
                self._error(f"commits.{sha}.{name}", GitHubError(f"unexpected response shape: {exc}"))
        if is_head and self.repo.outputs:
            outputs = {}
            for output in self.repo.outputs:
                try:
                    outputs[output.id] = self._output(sha, output)
                except GitHubError as exc:
                    self._error(f"commits.{sha}.outputs.{output.id}", exc)
            facts["outputs"] = outputs
        return facts

    def collect(self) -> dict[str, Any] | None:
        try:
            identity = self.api.get(self.base)
        except GitHubError as exc:
            self._error("repository", exc)
            return None
        if str(identity.get("full_name", "")).lower() != self.repo.github.lower():
            self._error(
                "repository",
                GitHubError(f"'{self.repo.github}' resolves to '{identity.get('full_name')}' (renamed or transferred)"),
            )
            return None

        head = self._head()
        branches: dict[str, Any] = {}
        merge_commits: set[str] = set()
        for task in sorted(self.tasks, key=lambda t: t.id):
            branch = task_branch(self.repo, task)
            pulls = self._pull_requests(branch)
            if pulls is None:
                continue
            branches[branch] = {"pullRequests": pulls}
            merge_commits.update(
                p["mergeCommitSha"]
                for p in pulls
                if p["state"] == "merged" and p["baseBranch"] == self.repo.integration_branch and p["mergeCommitSha"]
            )

        commits = {
            sha: self._commit(sha, head, sha == head) for sha in sorted(merge_commits | ({head} if head else set()))
        }

        facts: dict[str, Any] = {"branches": branches, "commits": commits}
        if head is not None:
            final = self._head()
            if final == head:
                facts["integrationHead"] = head
            else:
                # The branch moved while we were reading: reachability was judged against a head
                # that is no longer current, so neither fact can be trusted.
                self.errors.append(
                    CollectionError(self.repo.id, "integrationHead", f"moved from {head} to {final} during collection")
                )
                for commit in commits.values():
                    commit.pop("reachableFromIntegrationHead", None)
        return facts


def collect(project: Project, api: GitHubApi, now: Callable[[], datetime] | None = None) -> CollectionResult:
    """Collect evidence for every repository and task in a validated project."""
    clock = now or (lambda: datetime.now(UTC))
    observed_at = clock().astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    errors: list[CollectionError] = []
    repositories: dict[str, Any] = {}
    for repo in sorted(project.manifest.repositories, key=lambda r: r.id):
        tasks = [t for t in project.tasks.values() if t.repository == repo.id]
        facts = _RepositoryCollector(api, repo, tasks, errors).collect()
        if facts is not None:
            repositories[repo.id] = facts
    approvals, approval_errors = collect_approvals(project, api)
    errors.extend(CollectionError(project.manifest.planning_repository, f, m) for f, m in approval_errors)
    isolation = credential_isolation(api, project.manifest.approvers)

    errors.sort()
    snapshot: dict[str, Any] = {
        "approvals": approvals,
        "collection": {
            "collector": f"factory {__version__}",
            "complete": not errors,
            "errors": [e.to_dict() for e in errors],
        },
        "credentialIsolation": isolation,
        "observedAt": observed_at,
        "project": project.manifest.project_id,
        "provenance": {"kind": "github", "note": APPROVALS_NOTE},
        "repositories": repositories,
        "schemaVersion": "factory.evidence-snapshot/v1",
    }
    problems = validate_document("evidence", snapshot, "<collected snapshot>")
    if problems:
        raise RuntimeError("collector produced an invalid snapshot: " + "; ".join(p.render() for p in problems))
    return CollectionResult(snapshot, errors)
