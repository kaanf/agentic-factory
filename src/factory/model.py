"""Immutable domain model built from validated project documents."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# Ordered: each level includes every level before it.
LEVELS: tuple[str, ...] = ("approved", "merged", "validated", "integration-ready", "environment-verified")

PROFILE_EXEMPT_KINDS: frozenset[str] = frozenset({"spike"})

DEFAULT_CHECK_APP = "github-actions"


def level_index(level: str) -> int:
    return LEVELS.index(level)


@dataclass(frozen=True)
class RequiredCheck:
    name: str
    workflow_path: str
    app: str


@dataclass(frozen=True)
class Output:
    id: str
    path: str
    version_pointer: str


@dataclass(frozen=True)
class Repository:
    id: str
    github: str
    integration_branch: str
    release_branch: str
    feature_prefix: str
    required_checks: tuple[RequiredCheck, ...]
    outputs: tuple[Output, ...]


@dataclass(frozen=True)
class ProfileRef:
    id: str
    repository: str
    path: str


@dataclass(frozen=True)
class Manifest:
    file: str
    project_id: str
    approvers: tuple[str, ...]
    planning_repository: str
    repositories: tuple[Repository, ...]
    adr_directory: str
    profile_refs: tuple[ProfileRef, ...]
    dispatch_enabled: bool
    # Where the project directory sits inside the planning repository ("" for the repository root).
    planning_directory: str = ""

    def repository(self, repo_id: str) -> Repository | None:
        return next((repo for repo in self.repositories if repo.id == repo_id), None)

    def output_owner(self, output_id: str) -> Repository | None:
        return next((repo for repo in self.repositories if any(o.id == output_id for o in repo.outputs)), None)

    def profile_ref(self, profile_id: str) -> ProfileRef | None:
        return next((ref for ref in self.profile_refs if ref.id == profile_id), None)


@dataclass(frozen=True)
class Edge:
    task: str
    gate: str
    requires: str


@dataclass(frozen=True)
class Consumption:
    output: str
    version: str


@dataclass(frozen=True)
class Task:
    file: str
    id: str
    revision: int
    title: str
    repository: str
    kind: str
    spec_path: str
    spec_sha256: str
    profile: str | None
    profile_revision: int | None
    provides: tuple[str, ...]
    consumes: tuple[Consumption, ...]
    depends_on: tuple[Edge, ...]
    acceptance: tuple[str, ...]

    @property
    def requires_profile(self) -> bool:
        return self.kind not in PROFILE_EXEMPT_KINDS


@dataclass(frozen=True)
class AdrPin:
    id: str
    revision: int


@dataclass(frozen=True)
class Profile:
    file: str
    id: str
    revision: int
    repository: str
    adrs: tuple[AdrPin, ...]
    enforced_by: tuple[str, ...]
    manual_only: tuple[str, ...]


@dataclass(frozen=True)
class Adr:
    file: str
    id: str
    title: str
    status: str
    revision: int
    supersedes: str | None
    research: tuple[str, ...]
    critique: tuple[str, ...]
    constraint_ids: tuple[str, ...]


@dataclass(frozen=True)
class Project:
    """A fully loaded project.

    ``digests`` maps project-relative paths to SHA-256 of their bytes. ``rejected_task_ids`` holds
    ids of task files that failed validation, so references to them are not reported a second time.
    """

    root: Path
    manifest: Manifest
    tasks: dict[str, Task]
    profiles: dict[str, Profile]
    adrs: dict[str, Adr]
    digests: dict[str, str] = field(default_factory=dict)
    rejected_task_ids: frozenset[str] = frozenset()
