"""Loads a project directory into the domain model. Read-only: never writes to the project.

Layout (relative to the project directory, which is the planning repository root):

    factory/project.yaml        manifest
    factory/tasks/*.yaml        one task per file
    <profile paths>             as listed in the manifest
    <adrDirectory>/*.md         ADRs with YAML front matter
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from factory.documents import (
    DocumentError,
    parse_yaml,
    read_text,
    relative_name,
    resolve_inside,
    sha256_file,
    split_front_matter,
)
from factory.issues import Issue, join_field
from factory.model import (
    DEFAULT_CHECK_APP,
    Adr,
    AdrPin,
    Consumption,
    Edge,
    Manifest,
    Output,
    Profile,
    ProfileRef,
    Project,
    Repository,
    RequiredCheck,
    Task,
)
from factory.schemas.validation import validate_document

MANIFEST_PATH = "factory/project.yaml"
TASKS_DIRECTORY = "factory/tasks"

# Keys that would let a file declare its own approval. Approval must come from evidence.
_SELF_APPROVAL_KEY = re.compile(r"^(approv|signoff|signedoff|sign_off|signed_off|sign-off|signed-off)", re.IGNORECASE)


@dataclass
class LoadResult:
    project: Project | None
    errors: list[Issue] = field(default_factory=list)
    warnings: list[Issue] = field(default_factory=list)


def _self_declared_approvals(document: Any, file: str, path: str = "") -> list[Issue]:
    issues: list[Issue] = []
    if isinstance(document, dict):
        for key, value in document.items():
            child = join_field(path, str(key))
            if isinstance(key, str) and _SELF_APPROVAL_KEY.match(key):
                issues.append(
                    Issue(
                        file,
                        child,
                        "SELF_DECLARED_APPROVAL",
                        f"'{key}' is not accepted: approval is derived from verified evidence of a human merge, "
                        "never from file contents",
                    )
                )
            else:
                issues.extend(_self_declared_approvals(value, file, child))
    elif isinstance(document, list):
        for index, item in enumerate(document):
            issues.extend(_self_declared_approvals(item, file, join_field(path, index)))
    return issues


class _Loader:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.errors: list[Issue] = []
        self.digests: dict[str, str] = {}

    def error(self, file: str, field_: str, code: str, message: str) -> None:
        self.errors.append(Issue(file, field_, code, message))

    def parse(self, path: Path, *, front_matter: bool = False) -> Any | None:
        name = relative_name(self.root, path)
        if not path.resolve().is_relative_to(self.root):
            self.error(name, "", "PATH_OUTSIDE_PROJECT", "file resolves outside the project directory (symbolic link)")
            return None
        try:
            text = read_text(path)
            self.digests[name] = sha256_file(path)
            if front_matter:
                block = split_front_matter(text)
                if block is None:
                    raise DocumentError("MISSING_FRONT_MATTER", "file has no YAML front matter")
                return parse_yaml(block)
            return parse_yaml(text)
        except DocumentError as exc:
            self.error(name, exc.field, exc.code, exc.message)
            return None
        except OSError as exc:
            self.error(name, "", "UNREADABLE_FILE", f"cannot read file: {exc.strerror}")
            return None

    def checked(self, kind: str, document: Any, file: str, *, reject_approvals: bool) -> bool:
        issues: list[Issue] = []
        approval_fields: set[str] = set()
        if reject_approvals:
            approvals = _self_declared_approvals(document, file)
            approval_fields = {issue.field for issue in approvals}
            issues.extend(approvals)
        for issue in validate_document(kind, document, file):
            if issue.code == "UNKNOWN_FIELD" and issue.field in approval_fields:
                continue
            issues.append(issue)
        self.errors.extend(issues)
        return not issues


def _manifest(document: dict[str, Any], file: str) -> Manifest:
    repositories = []
    for repo in document["repositories"]:
        branches = repo["branches"]
        checks = tuple(
            RequiredCheck(c["name"], c["workflowPath"], c.get("app", DEFAULT_CHECK_APP))
            for c in repo["requiredChecks"]["integration"]
        )
        outputs = tuple(Output(o["id"], o["path"], o["versionPointer"]) for o in repo.get("outputs", []))
        repositories.append(
            Repository(
                id=repo["id"],
                github=repo["github"],
                integration_branch=branches["integration"],
                release_branch=branches["release"],
                feature_prefix=branches["featurePrefix"],
                required_checks=checks,
                outputs=outputs,
            )
        )
    architecture = document["architecture"]
    return Manifest(
        file=file,
        project_id=document["project"]["id"],
        approvers=tuple(document["approvers"]),
        planning_repository=document["planningRepository"],
        repositories=tuple(repositories),
        adr_directory=architecture["adrDirectory"],
        profile_refs=tuple(ProfileRef(p["id"], p["repository"], p["path"]) for p in architecture["profiles"]),
        dispatch_enabled=bool(document["dispatch"].get("enabled", False)),
        planning_directory=document.get("planningDirectory", ""),
    )


def _task(document: dict[str, Any], file: str) -> Task:
    architecture = document.get("architecture")
    return Task(
        file=file,
        id=document["id"],
        revision=document["revision"],
        title=document["title"],
        repository=document["repository"],
        kind=document["kind"],
        spec_path=document["specification"]["path"],
        spec_sha256=document["specification"]["sha256"],
        profile=architecture["profile"] if architecture else None,
        profile_revision=architecture["profileRevision"] if architecture else None,
        provides=tuple(document.get("provides", [])),
        consumes=tuple(Consumption(c["output"], c["version"]) for c in document.get("consumes", [])),
        depends_on=tuple(Edge(e["task"], e["gate"], e["requires"]) for e in document.get("dependsOn", [])),
        acceptance=tuple(document["acceptance"]),
    )


def _profile(document: dict[str, Any], file: str) -> Profile:
    return Profile(
        file=file,
        id=document["id"],
        revision=document["revision"],
        repository=document["repository"],
        adrs=tuple(AdrPin(a["id"], a["revision"]) for a in document["adrs"]),
        enforced_by=tuple(e["check"] for e in document.get("enforcedBy", [])),
        manual_only=tuple(document.get("manualOnly", [])),
    )


def _adr(document: dict[str, Any], file: str) -> Adr:
    return Adr(
        file=file,
        id=document["id"],
        title=document["title"],
        status=document["status"],
        revision=document["revision"],
        supersedes=document.get("supersedes"),
        research=tuple(document.get("research", [])),
        critique=tuple(document.get("critique", [])),
        constraint_ids=tuple(c["id"] for c in document.get("constraints", [])),
    )


def load_project(directory: Path) -> LoadResult:
    """Parse and schema-check every project document. Cross-references are checked in ``validation``."""
    if not directory.is_dir():
        return LoadResult(None, [Issue("", "", "PROJECT_NOT_FOUND", f"project directory not found: {directory}")])
    root = directory.resolve()
    loader = _Loader(root)

    manifest_path = root / MANIFEST_PATH
    if not manifest_path.is_file():
        loader.error(MANIFEST_PATH, "", "MANIFEST_NOT_FOUND", f"manifest not found; expected {MANIFEST_PATH}")
        return LoadResult(None, loader.errors)
    manifest_doc = loader.parse(manifest_path)
    manifest: Manifest | None = None
    if manifest_doc is not None and loader.checked("project", manifest_doc, MANIFEST_PATH, reject_approvals=False):
        manifest = _manifest(manifest_doc, MANIFEST_PATH)

    tasks: dict[str, Task] = {}
    rejected: set[str] = set()
    task_dir = root / TASKS_DIRECTORY
    if task_dir.is_dir():
        for path in sorted(task_dir.iterdir()):
            name = relative_name(root, path)
            if path.suffix == ".yml":
                loader.error(name, "", "UNSUPPORTED_EXTENSION", "task files must use the .yaml extension")
                continue
            if path.suffix != ".yaml" or not path.is_file():
                continue
            document = loader.parse(path)
            if document is None or not loader.checked("task", document, name, reject_approvals=True):
                if isinstance(document, dict) and isinstance(document.get("id"), str):
                    rejected.add(document["id"])
                continue
            task = _task(document, name)
            if task.id in tasks:
                loader.error(
                    name, "id", "DUPLICATE_TASK_ID", f"task id '{task.id}' is already defined in {tasks[task.id].file}"
                )
                continue
            tasks[task.id] = task

    # Specification digests are keyed by the path exactly as tasks reference it.
    for task in tasks.values():
        spec = resolve_inside(root, task.spec_path)
        if spec is not None and spec.is_file():
            loader.digests[task.spec_path] = sha256_file(spec)

    profiles: dict[str, Profile] = {}
    adrs: dict[str, Adr] = {}
    if manifest is not None:
        for index, ref in enumerate(manifest.profile_refs):
            field_ = f"architecture.profiles[{index}].path"
            path = resolve_inside(root, ref.path)
            if path is None:
                loader.error(
                    MANIFEST_PATH, field_, "PATH_OUTSIDE_PROJECT", f"'{ref.path}' escapes the project directory"
                )
                continue
            if not path.is_file():
                loader.error(MANIFEST_PATH, field_, "PROFILE_NOT_FOUND", f"profile file not found: {ref.path}")
                continue
            name = relative_name(root, path)
            document = loader.parse(path)
            if document is None or not loader.checked("profile", document, name, reject_approvals=True):
                continue
            profile = _profile(document, name)
            if profile.id != ref.id:
                loader.error(
                    name,
                    "id",
                    "PROFILE_ID_MISMATCH",
                    f"profile id '{profile.id}' does not match manifest id '{ref.id}'",
                )
                continue
            if profile.id in profiles:
                continue  # duplicate manifest ids are reported by cross-reference validation
            profiles[profile.id] = profile

        adr_dir = resolve_inside(root, manifest.adr_directory)
        if adr_dir is None:
            loader.error(
                MANIFEST_PATH,
                "architecture.adrDirectory",
                "PATH_OUTSIDE_PROJECT",
                f"'{manifest.adr_directory}' escapes the project directory",
            )
        elif adr_dir.is_dir():
            for path in sorted(adr_dir.glob("*.md")):
                if not path.is_file() or not _starts_front_matter(path):
                    continue
                name = relative_name(root, path)
                document = loader.parse(path, front_matter=True)
                if document is None or not loader.checked("adr", document, name, reject_approvals=True):
                    continue
                adr = _adr(document, name)
                if adr.id in adrs:
                    loader.error(
                        name, "id", "DUPLICATE_ADR_ID", f"ADR id '{adr.id}' is already defined in {adrs[adr.id].file}"
                    )
                    continue
                adrs[adr.id] = adr

    if manifest is None:
        return LoadResult(None, loader.errors)
    project = Project(
        root, manifest, tasks, profiles, adrs, dict(sorted(loader.digests.items())), frozenset(rejected - set(tasks))
    )
    return LoadResult(project, loader.errors)


def _starts_front_matter(path: Path) -> bool:
    """True if the Markdown file starts a front matter block. Files without one are not ADRs."""
    try:
        with path.open("rb") as handle:
            first = handle.readline()
    except OSError:
        return True  # let the parser report the read error
    return first.rstrip(b"\r\n") == b"---"
