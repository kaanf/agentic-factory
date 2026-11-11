"""Evidence snapshot loading and validation. The snapshot is data only; nothing here touches the network."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from factory.documents import DocumentError, parse_json, read_text
from factory.issues import Issue
from factory.model import Project
from factory.schemas.validation import validate_document


@dataclass(frozen=True)
class Approval:
    path: str
    sha256: str
    approved: bool
    merged_by: str | None


@dataclass(frozen=True)
class PullRequest:
    number: int
    state: str
    base_branch: str
    merge_commit_sha: str | None


@dataclass(frozen=True)
class CheckRun:
    name: str
    app: str
    event: str
    head_branch: str
    workflow_path: str
    status: str
    conclusion: str | None
    attempt: int


@dataclass(frozen=True)
class OutputFact:
    present: bool
    version: str | None


@dataclass(frozen=True)
class CommitFacts:
    """Each attribute is None when the snapshot does not state the fact (UNKNOWN)."""

    reachable: bool | None
    merged_pull_requests: tuple[int, ...] | None
    check_runs: tuple[CheckRun, ...] | None
    outputs: dict[str, OutputFact] | None


@dataclass(frozen=True)
class RepositoryFacts:
    integration_head: str | None
    branches: dict[str, tuple[PullRequest, ...]]
    commits: dict[str, CommitFacts]


@dataclass(frozen=True)
class Snapshot:
    file: str
    project: str
    provenance: str
    observed_at: str | None
    approvals: dict[str, Approval]
    repositories: dict[str, RepositoryFacts] = field(default_factory=dict)

    @property
    def authenticated(self) -> bool:
        # A snapshot is a local file: even one written by the GitHub collector can be edited
        # afterwards, and approvals are not collected yet. Nothing in v1 is authenticated.
        return False


@dataclass
class SnapshotResult:
    snapshot: Snapshot | None
    errors: list[Issue]


def _commit(raw: dict[str, Any]) -> CommitFacts:
    runs = raw.get("checkRuns")
    outputs = raw.get("outputs")
    merged = raw.get("mergedPullRequests")
    return CommitFacts(
        reachable=raw.get("reachableFromIntegrationHead"),
        merged_pull_requests=tuple(merged) if merged is not None else None,
        check_runs=tuple(
            CheckRun(
                r["name"],
                r["app"],
                r["event"],
                r["headBranch"],
                r["workflowPath"],
                r["status"],
                r["conclusion"],
                r["attempt"],
            )
            for r in runs
        )
        if runs is not None
        else None,
        outputs={k: OutputFact(v["present"], v.get("version")) for k, v in outputs.items()}
        if outputs is not None
        else None,
    )


def _repository(raw: dict[str, Any]) -> RepositoryFacts:
    return RepositoryFacts(
        integration_head=raw.get("integrationHead"),
        branches={
            name: tuple(
                PullRequest(p["number"], p["state"], p["baseBranch"], p.get("mergeCommitSha"))
                for p in entry["pullRequests"]
            )
            for name, entry in raw.get("branches", {}).items()
        },
        commits={sha: _commit(facts) for sha, facts in raw.get("commits", {}).items()},
    )


def load_snapshot(path: Path, project: Project) -> SnapshotResult:
    file = path.as_posix()
    if not path.is_file():
        return SnapshotResult(None, [Issue(file, "", "EVIDENCE_NOT_FOUND", "evidence snapshot file not found")])
    try:
        document = parse_json(read_text(path))
    except DocumentError as exc:
        return SnapshotResult(None, [Issue(file, exc.field, exc.code, exc.message)])
    except OSError as exc:
        return SnapshotResult(None, [Issue(file, "", "UNREADABLE_FILE", f"cannot read file: {exc.strerror}")])

    errors = validate_document("evidence", document, file)
    if errors:
        return SnapshotResult(None, errors)

    if document["project"] != project.manifest.project_id:
        errors.append(
            Issue(
                file,
                "project",
                "SNAPSHOT_PROJECT_MISMATCH",
                f"snapshot is for project '{document['project']}', manifest is '{project.manifest.project_id}'",
            )
        )
    for repo_id in sorted(document["repositories"]):
        if project.manifest.repository(repo_id) is None:
            errors.append(
                Issue(
                    file,
                    f"repositories.{repo_id}",
                    "UNKNOWN_REPOSITORY",
                    f"'{repo_id}' is not a repository id in the manifest",
                )
            )
    for approval_path, count in sorted(Counter(a["path"] for a in document["approvals"]).items()):
        if count > 1:
            errors.append(
                Issue(
                    file, "approvals", "AMBIGUOUS_APPROVAL", f"approval for '{approval_path}' is listed {count} times"
                )
            )
    if errors:
        return SnapshotResult(None, sorted(errors))

    snapshot = Snapshot(
        file=file,
        project=document["project"],
        provenance=document["provenance"]["kind"],
        observed_at=document.get("observedAt"),
        approvals={
            a["path"]: Approval(a["path"], a["sha256"], a["approved"], a.get("mergedBy")) for a in document["approvals"]
        },
        repositories={repo_id: _repository(raw) for repo_id, raw in document["repositories"].items()},
    )
    return SnapshotResult(snapshot, [])
