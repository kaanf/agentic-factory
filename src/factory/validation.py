"""Cross-reference validation over a loaded project. Pure apart from file existence checks."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from factory.documents import resolve_inside
from factory.graph import cycle_path, find_cycles
from factory.issues import Issue
from factory.model import Project
from factory.project import LoadResult, load_project
from factory.semver import VersionError, parse_range


def _file_reference(root: Path, relative: str) -> str | None:
    """Return an error code if a referenced project file is unusable, else None."""
    path = resolve_inside(root, relative)
    if path is None:
        return "PATH_OUTSIDE_PROJECT"
    if not path.is_file():
        return "REFERENCED_FILE_NOT_FOUND"
    return None


def _manifest_issues(project: Project) -> tuple[list[Issue], list[Issue]]:
    manifest = project.manifest
    file = manifest.file
    errors: list[Issue] = []
    warnings: list[Issue] = []

    for repo_id, count in sorted(Counter(r.id for r in manifest.repositories).items()):
        if count > 1:
            errors.append(
                Issue(
                    file,
                    "repositories",
                    "DUPLICATE_REPOSITORY_ID",
                    f"repository id '{repo_id}' is defined {count} times",
                )
            )
    for github, count in sorted(Counter(r.github.lower() for r in manifest.repositories).items()):
        if count > 1:
            errors.append(
                Issue(
                    file,
                    "repositories",
                    "DUPLICATE_GITHUB_REPOSITORY",
                    f"GitHub repository '{github}' is listed {count} times",
                )
            )
    if manifest.repository(manifest.planning_repository) is None:
        errors.append(
            Issue(
                file,
                "planningRepository",
                "UNKNOWN_REPOSITORY",
                f"'{manifest.planning_repository}' is not a repository id in this manifest",
            )
        )

    output_ids = Counter(o.id for r in manifest.repositories for o in r.outputs)
    for output_id, count in sorted(output_ids.items()):
        if count > 1:
            errors.append(
                Issue(file, "repositories", "DUPLICATE_OUTPUT_ID", f"output id '{output_id}' is declared {count} times")
            )

    for index, repo in enumerate(manifest.repositories):
        base = f"repositories[{index}]"
        if repo.integration_branch == repo.release_branch:
            errors.append(
                Issue(file, f"{base}.branches", "AMBIGUOUS_BRANCHES", "integration and release branches must differ")
            )
        for branch in (repo.integration_branch, repo.release_branch):
            if branch.startswith(repo.feature_prefix):
                errors.append(
                    Issue(
                        file,
                        f"{base}.branches.featurePrefix",
                        "AMBIGUOUS_BRANCHES",
                        f"protected branch '{branch}' starts with feature prefix '{repo.feature_prefix}'",
                    )
                )
        for name, count in sorted(Counter(c.name for c in repo.required_checks).items()):
            if count > 1:
                errors.append(
                    Issue(
                        file,
                        f"{base}.requiredChecks.integration",
                        "DUPLICATE_CHECK",
                        f"check '{name}' is listed {count} times",
                    )
                )
        if not repo.required_checks:
            warnings.append(
                Issue(
                    file,
                    f"{base}.requiredChecks.integration",
                    "NO_REQUIRED_CHECKS",
                    f"repository '{repo.id}' has no required integration checks, so its tasks can never reach 'validated'",
                )
            )

    for profile_id, count in sorted(Counter(p.id for p in manifest.profile_refs).items()):
        if count > 1:
            errors.append(
                Issue(
                    file,
                    "architecture.profiles",
                    "DUPLICATE_PROFILE_ID",
                    f"profile id '{profile_id}' is listed {count} times",
                )
            )
    for path, count in sorted(Counter(p.path for p in manifest.profile_refs).items()):
        if count > 1:
            errors.append(
                Issue(
                    file,
                    "architecture.profiles",
                    "DUPLICATE_PROFILE_PATH",
                    f"profile path '{path}' is listed {count} times",
                )
            )
    for index, ref in enumerate(manifest.profile_refs):
        if manifest.repository(ref.repository) is None:
            errors.append(
                Issue(
                    file,
                    f"architecture.profiles[{index}].repository",
                    "UNKNOWN_REPOSITORY",
                    f"'{ref.repository}' is not a repository id in this manifest",
                )
            )

    if manifest.dispatch_enabled:
        errors.append(
            Issue(
                file,
                "dispatch.enabled",
                "DISPATCH_NOT_SUPPORTED",
                "this Factory Core version cannot dispatch agents; dispatch.enabled must be false",
            )
        )
    return errors, warnings


def _task_issues(project: Project) -> tuple[list[Issue], list[Issue]]:
    manifest = project.manifest
    errors: list[Issue] = []
    warnings: list[Issue] = []

    for task in sorted(project.tasks.values(), key=lambda t: t.id):
        file = task.file
        repo = manifest.repository(task.repository)
        if repo is None:
            errors.append(
                Issue(
                    file,
                    "repository",
                    "UNKNOWN_REPOSITORY",
                    f"'{task.repository}' is not a repository id in the manifest",
                )
            )

        code = _file_reference(project.root, task.spec_path)
        if code is not None:
            errors.append(Issue(file, "specification.path", code, f"specification file not usable: {task.spec_path}"))

        if task.profile is not None:
            ref = manifest.profile_ref(task.profile)
            if ref is None:
                errors.append(
                    Issue(
                        file,
                        "architecture.profile",
                        "UNKNOWN_PROFILE",
                        f"profile '{task.profile}' is not listed in the manifest",
                    )
                )
            elif ref.repository != task.repository:
                errors.append(
                    Issue(
                        file,
                        "architecture.profile",
                        "PROFILE_REPOSITORY_MISMATCH",
                        f"profile '{task.profile}' targets repository '{ref.repository}', but the task targets '{task.repository}'",
                    )
                )
            profile = project.profiles.get(task.profile)
            if profile is not None and task.profile_revision != profile.revision:
                warnings.append(
                    Issue(
                        file,
                        "architecture.profileRevision",
                        "PROFILE_REVISION_MISMATCH",
                        f"task pins profile revision {task.profile_revision}, profile is at revision {profile.revision}; "
                        "the task will plan as STALE",
                    )
                )

        for index, output_id in enumerate(task.provides):
            owner = manifest.output_owner(output_id)
            if owner is None:
                errors.append(
                    Issue(
                        file,
                        f"provides[{index}]",
                        "UNKNOWN_OUTPUT",
                        f"output '{output_id}' is not declared in the manifest",
                    )
                )
            elif owner.id != task.repository:
                errors.append(
                    Issue(
                        file,
                        f"provides[{index}]",
                        "OUTPUT_REPOSITORY_MISMATCH",
                        f"output '{output_id}' belongs to repository '{owner.id}', not '{task.repository}'",
                    )
                )

        for index, consumption in enumerate(task.consumes):
            if manifest.output_owner(consumption.output) is None:
                errors.append(
                    Issue(
                        file,
                        f"consumes[{index}].output",
                        "UNKNOWN_OUTPUT",
                        f"output '{consumption.output}' is not declared in the manifest",
                    )
                )
            try:
                parse_range(consumption.version)
            except VersionError as exc:
                errors.append(Issue(file, f"consumes[{index}].version", "INVALID_VERSION_RANGE", str(exc)))
            providers = {
                edge.task
                for edge in task.depends_on
                if consumption.output in getattr(project.tasks.get(edge.task), "provides", ())
            }
            if not providers:
                warnings.append(
                    Issue(
                        file,
                        f"consumes[{index}].output",
                        "CONSUMED_OUTPUT_UNLINKED",
                        f"no dependency of this task provides '{consumption.output}', so its version is never checked",
                    )
                )

        seen: dict[tuple[str, str], int] = {}
        for index, edge in enumerate(task.depends_on):
            field = f"dependsOn[{index}].task"
            if edge.task == task.id:
                errors.append(Issue(file, field, "SELF_DEPENDENCY", f"task '{task.id}' depends on itself"))
            elif edge.task not in project.tasks and edge.task not in project.rejected_task_ids:
                errors.append(
                    Issue(file, field, "UNKNOWN_DEPENDENCY", f"dependency '{edge.task}' is not a task in this project")
                )
            key = (edge.task, edge.gate)
            if key in seen:
                errors.append(
                    Issue(
                        file,
                        f"dependsOn[{index}]",
                        "DUPLICATE_DEPENDENCY",
                        f"'{edge.task}' is already a '{edge.gate}' dependency at dependsOn[{seen[key]}]",
                    )
                )
            else:
                seen[key] = index

    graph = {task_id: [e.task for e in task.depends_on if e.task != task_id] for task_id, task in project.tasks.items()}
    for component in find_cycles(graph):
        path = cycle_path(graph, component)
        files = sorted({project.tasks[m].file for m in component})
        errors.append(
            Issue(
                files[0],
                "dependsOn",
                "DEPENDENCY_CYCLE",
                f"dependency cycle between tasks {', '.join(component)}: {' -> '.join(path)} (files: {', '.join(files)})",
            )
        )
    return errors, warnings


def _architecture_issues(project: Project) -> tuple[list[Issue], list[Issue]]:
    manifest = project.manifest
    errors: list[Issue] = []
    warnings: list[Issue] = []

    for profile in sorted(project.profiles.values(), key=lambda p: p.id):
        file = profile.file
        ref = manifest.profile_ref(profile.id)
        if ref is not None and ref.repository != profile.repository:
            errors.append(
                Issue(
                    file,
                    "repository",
                    "PROFILE_REPOSITORY_MISMATCH",
                    f"profile targets '{profile.repository}', but the manifest lists it for '{ref.repository}'",
                )
            )
        repo = manifest.repository(profile.repository)
        if repo is None:
            errors.append(
                Issue(
                    file,
                    "repository",
                    "UNKNOWN_REPOSITORY",
                    f"'{profile.repository}' is not a repository id in the manifest",
                )
            )

        seen: set[str] = set()
        constraint_ids: set[str] = set()
        for index, pin in enumerate(profile.adrs):
            field = f"adrs[{index}]"
            if pin.id in seen:
                errors.append(
                    Issue(file, f"{field}.id", "DUPLICATE_ADR_REFERENCE", f"ADR '{pin.id}' is listed more than once")
                )
            seen.add(pin.id)
            adr = project.adrs.get(pin.id)
            if adr is None:
                errors.append(
                    Issue(
                        file,
                        f"{field}.id",
                        "ADR_NOT_FOUND",
                        f"ADR '{pin.id}' was not found in {manifest.adr_directory}",
                    )
                )
                continue
            constraint_ids.update(adr.constraint_ids)
            if adr.revision != pin.revision:
                warnings.append(
                    Issue(
                        file,
                        f"{field}.revision",
                        "ADR_REVISION_MISMATCH",
                        f"profile pins {pin.id} revision {pin.revision}, ADR is at revision {adr.revision}; "
                        "dependent tasks will plan as STALE",
                    )
                )
            if adr.status != "accepted":
                warnings.append(
                    Issue(
                        file,
                        f"{field}.id",
                        "ADR_NOT_ACCEPTED",
                        f"ADR '{pin.id}' has status '{adr.status}'; dependent tasks will plan as BLOCKED",
                    )
                )

        check_names = {c.name for c in repo.required_checks} if repo is not None else set()
        for index, check in enumerate(profile.enforced_by):
            if repo is not None and check not in check_names:
                errors.append(
                    Issue(
                        file,
                        f"enforcedBy[{index}].check",
                        "UNKNOWN_CHECK",
                        f"check '{check}' is not a required integration check of repository '{profile.repository}'",
                    )
                )
        for index, constraint in enumerate(profile.manual_only):
            if constraint not in constraint_ids:
                errors.append(
                    Issue(
                        file,
                        f"manualOnly[{index}]",
                        "UNKNOWN_CONSTRAINT",
                        f"constraint '{constraint}' is not defined by any ADR this profile lists",
                    )
                )

    for adr in sorted(project.adrs.values(), key=lambda a: a.id):
        for field, paths in (("research", adr.research), ("critique", adr.critique)):
            for index, path in enumerate(paths):
                code = _file_reference(project.root, path)
                if code is not None:
                    errors.append(Issue(adr.file, f"{field}[{index}]", code, f"{field} document not usable: {path}"))
        for constraint_id, count in sorted(Counter(adr.constraint_ids).items()):
            if count > 1:
                errors.append(
                    Issue(
                        adr.file,
                        "constraints",
                        "DUPLICATE_CONSTRAINT_ID",
                        f"constraint id '{constraint_id}' is defined {count} times",
                    )
                )
        if adr.supersedes is not None and adr.supersedes not in project.adrs:
            warnings.append(
                Issue(adr.file, "supersedes", "ADR_NOT_FOUND", f"superseded ADR '{adr.supersedes}' was not found")
            )
    return errors, warnings


def validate_project(directory: Path) -> LoadResult:
    """Load the project and run every structural and cross-reference check."""
    result = load_project(directory)
    if result.project is not None:
        for check in (_manifest_issues, _task_issues, _architecture_issues):
            errors, warnings = check(result.project)
            result.errors.extend(errors)
            result.warnings.extend(warnings)
    result.errors = sorted(set(result.errors))
    result.warnings = sorted(set(result.warnings))
    return result
