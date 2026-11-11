"""Archon adapter: workflow and provider policy, Archon state readers, and the command builder.

Nothing here starts a process. ``build_command`` returns the arguments a future dispatch would pass
to ``archon`` as a structured argument vector (never a shell string); ``display`` quotes it for
reading only. Archon's installed version and configuration are read from its files, not by running
it.

Policy is fixed in code and can only be narrowed by a manifest:

- A workflow must be listed in the manifest, be in ``SUPPORTED_WORKFLOWS`` (workflows whose behavior
  this adapter was written against) and must not be denied: ``DENIED_WORKFLOWS`` and any name
  containing a word from ``DENIED_WORDS`` (merge, queue, deploy, ...) are refused even when a
  manifest allows them. A manifest's ``forbidden`` list adds to the denylist.
- The command names ``--branch`` and ``--base`` explicitly and nothing else: no detached runs, no
  run without a worktree, no adoption or resumption of other runs, no configuration layers, no
  model or provider rebinding, no workflow inputs.
- Archon 0.14 has no verified per-run provider selection (``workflow run`` offers ``--model``
  bindings and configuration layers, whose effect on the provider was not verified). The task's
  provider must therefore be the one Archon is already configured to use by default.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from factory.documents import DocumentError, parse_json, parse_yaml
from factory.fsread import ConfinedTree, FsError
from factory.model import Dispatch, Repository, Task
from factory.refs import is_command_branch, is_name, is_task_id, is_version, unsafe_text

# Archon releases whose `workflow run` options this builder was checked against. A manifest pinned
# to any other release is refused until the adapter and the probe procedure are re-verified.
ADAPTER_ARCHON_VERSIONS: frozenset[str] = frozenset({"0.14.0"})

# Workflows whose effects were reviewed for this adapter. archon-implement commits on its branch,
# keeps working until its focused checks pass, and opens no pull request.
SUPPORTED_WORKFLOWS: frozenset[str] = frozenset({"archon-implement"})

# Never run, whatever a manifest says: queues merge or build, intake reads untrusted issue text,
# cleanup removes worktrees and branches.
DENIED_WORKFLOWS: frozenset[str] = frozenset(
    {"archon-merge-queue", "archon-build-queue", "archon-intake", "archon-worktree-cleanup"}
)
DENIED_WORDS: frozenset[str] = frozenset(
    {"merge", "queue", "deploy", "release", "ship", "publish", "intake", "trigger", "serve", "cleanup", "delete"}
)

EXECUTABLE = "archon"
# The only options the builder may emit. Tests assert this.
EMITTED_OPTIONS: frozenset[str] = frozenset({"--branch", "--base"})
ENVIRONMENT: tuple[tuple[str, str], ...] = (("DO_NOT_TRACK", "1"),)
MAX_PROMPT_BYTES = 16 * 1024

REPO_CONFIG = (".archon", "config.yaml")
_SMALL_FILE = 256 * 1024


@dataclass(frozen=True, order=True)
class Finding:
    """One policy or state finding. ``result`` is FAIL (definite) or UNKNOWN (could not be established)."""

    code: str
    result: str
    message: str
    ref: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "ref": self.ref, "result": self.result}


def _fail(code: str, message: str, ref: str = "") -> Finding:
    return Finding(code, "FAIL", message, ref)


def _unknown(code: str, message: str, ref: str = "") -> Finding:
    return Finding(code, "UNKNOWN", message, ref)


# -- policy ---------------------------------------------------------------------------------------


def denied_by_factory(workflow: str) -> bool:
    words = set(re.split(r"[._-]", workflow))
    return workflow in DENIED_WORKFLOWS or bool(words & DENIED_WORDS)


def resolve_workflow(task: Task, dispatch: Dispatch) -> tuple[str | None, list[Finding]]:
    """The workflow a task would run, and every reason it may not."""
    workflow = task.workflow if task.workflow is not None else dispatch.default_workflow
    if workflow is None:
        return None, [_fail("WORKFLOW_NOT_CONFIGURED", "neither the task nor the manifest names a workflow")]
    if not is_name(workflow):
        return None, [_fail("WORKFLOW_INVALID", f"workflow name {workflow!r} is not a plain name", workflow)]
    findings = []
    if denied_by_factory(workflow):
        findings.append(_fail("WORKFLOW_FORBIDDEN", f"workflow '{workflow}' is denied by Factory Core", workflow))
    if workflow in dispatch.forbidden_workflows:
        findings.append(_fail("WORKFLOW_FORBIDDEN", f"workflow '{workflow}' is forbidden by the manifest", workflow))
    if workflow not in dispatch.allowed_workflows:
        findings.append(
            _fail("WORKFLOW_NOT_ALLOWED", f"workflow '{workflow}' is not allowed by the manifest", workflow)
        )
    if workflow not in SUPPORTED_WORKFLOWS:
        supported = ", ".join(sorted(SUPPORTED_WORKFLOWS))
        findings.append(
            _fail(
                "WORKFLOW_NOT_SUPPORTED",
                f"workflow '{workflow}' is not supported by this adapter (supported: {supported})",
                workflow,
            )
        )
    return (None if findings else workflow), findings


def resolve_provider(task: Task, dispatch: Dispatch) -> tuple[str | None, str | None, list[Finding]]:
    """(provider, Archon assistant) a task would use, and every reason it may not."""
    provider = task.provider if task.provider is not None else dispatch.default_provider
    if provider is None:
        return None, None, [_fail("PROVIDER_NOT_CONFIGURED", "neither the task nor the manifest names a provider")]
    if not is_name(provider):
        return None, None, [_fail("PROVIDER_INVALID", f"provider name {provider!r} is not a plain name", provider)]
    assistant = dispatch.providers.get(provider)
    if assistant is None:
        return None, None, [_fail("PROVIDER_NOT_AVAILABLE", f"provider '{provider}' is not configured", provider)]
    if not is_name(assistant):
        return None, None, [_fail("PROVIDER_ASSISTANT_INVALID", f"assistant {assistant!r} is not a plain name")]
    return provider, assistant, []


def check_archon_version(dispatch: Dispatch, installed: str | None, problem: Finding | None) -> list[Finding]:
    pinned = dispatch.archon_version
    if pinned is None:
        return [_fail("ARCHON_VERSION_NOT_CONFIGURED", "the manifest does not pin a validated Archon version")]
    findings = []
    if pinned not in ADAPTER_ARCHON_VERSIONS:
        findings.append(
            _fail(
                "ARCHON_VERSION_UNSUPPORTED",
                f"the manifest pins Archon {pinned}, which this adapter was not verified against",
                pinned,
            )
        )
    if problem is not None:
        findings.append(problem)
    elif installed != pinned:
        findings.append(
            _fail("ARCHON_VERSION_MISMATCH", f"installed Archon is {installed}, the manifest pins {pinned}", pinned)
        )
    return findings


# -- Archon state, read from files ----------------------------------------------------------------


@dataclass(frozen=True)
class ArchonInstall:
    """What Archon's install record and global configuration state. Recorded claims, not a probe."""

    version: str | None
    default_assistant: str | None
    version_problem: Finding | None
    assistant_problem: Finding | None


def _read_json(tree: ConfinedTree, *parts: str) -> Any:
    return parse_json(tree.read(*parts, max_bytes=_SMALL_FILE).decode("utf-8"))


def read_archon_home(home: Path) -> ArchonInstall:
    """Read ``install.json`` (version) and ``config.yaml`` (default assistant) from Archon's home."""
    version: str | None = None
    assistant: str | None = None
    version_problem: Finding | None = None
    assistant_problem: Finding | None = None
    try:
        tree = ConfinedTree.open(home)
    except FsError as exc:
        problem = _unknown("ARCHON_HOME_UNREADABLE", f"Archon home: {exc.message}")
        return ArchonInstall(None, None, problem, problem)
    with tree:
        try:
            record = _read_json(tree, "install.json")
            value = record.get("version") if isinstance(record, dict) else None
            if is_version(value):
                version = value
            else:
                version_problem = _unknown("ARCHON_VERSION_UNKNOWN", "install.json has no valid 'version'")
        except (FsError, DocumentError, UnicodeDecodeError) as exc:
            version_problem = _unknown("ARCHON_VERSION_UNKNOWN", f"Archon install record: {_message(exc)}")
        try:
            config = parse_yaml(tree.read("config.yaml", max_bytes=_SMALL_FILE).decode("utf-8"))
            value = config.get("defaultAssistant") if isinstance(config, dict) else None
            if is_name(value):
                assistant = value
            else:
                assistant_problem = _unknown(
                    "ARCHON_DEFAULT_ASSISTANT_UNKNOWN", "Archon's config.yaml has no valid 'defaultAssistant'"
                )
        except (FsError, DocumentError, UnicodeDecodeError) as exc:
            assistant_problem = _unknown("ARCHON_DEFAULT_ASSISTANT_UNKNOWN", f"Archon config: {_message(exc)}")
    return ArchonInstall(version, assistant, version_problem, assistant_problem)


def _message(exc: Exception) -> str:
    return getattr(exc, "message", None) or str(exc)


def check_assistant(assistant: str | None, install: ArchonInstall) -> list[Finding]:
    if assistant is None:
        return []
    if install.assistant_problem is not None:
        return [install.assistant_problem]
    if install.default_assistant != assistant:
        return [
            _fail(
                "PROVIDER_NOT_ARCHON_DEFAULT",
                f"the task needs Archon assistant '{assistant}', but Archon's default is "
                f"'{install.default_assistant}' and per-run provider selection is not verified",
                assistant,
            )
        ]
    return []


def check_repo_config(text: str | None, problem: FsError | None, integration: str, where: str) -> list[Finding]:
    """Check an ``.archon/config.yaml`` for ``worktree.baseBranch`` equal to the integration branch."""
    prefix = "ARCHON_REMOTE" if where == "remote" else "ARCHON"
    label = f"{where} .archon/config.yaml"
    if problem is not None:
        if problem.code == "NOT_FOUND":
            return [_fail(f"{prefix}_REPO_CONFIG_MISSING", f"{label} does not exist")]
        if problem.code in ("SYMLINK", "HARD_LINKED", "NOT_A_FILE", "NOT_A_DIRECTORY", "TOO_LARGE"):
            return [_fail(f"{prefix}_REPO_CONFIG_UNSAFE", f"{label}: {problem.message}")]
        return [_unknown(f"{prefix}_REPO_CONFIG_UNREADABLE", f"{label}: {problem.message}")]
    assert text is not None
    try:
        document = parse_yaml(text)
    except DocumentError as exc:
        return [_fail(f"{prefix}_REPO_CONFIG_INVALID", f"{label}: {exc.message}")]
    worktree = document.get("worktree") if isinstance(document, dict) else None
    base = worktree.get("baseBranch") if isinstance(worktree, dict) else None
    if base is None:
        return [_fail(f"{prefix}_BASE_BRANCH_UNSET", f"{label} does not set worktree.baseBranch")]
    if base != integration:
        return [
            _fail(
                f"{prefix}_BASE_BRANCH_MISMATCH",
                f"{label} sets worktree.baseBranch {base!r}, the integration branch is '{integration}'",
                integration,
            )
        ]
    return []


def read_local_repo_config(checkout: Path) -> tuple[str | None, FsError | None]:
    try:
        with ConfinedTree.open(checkout) as tree:
            return tree.read(*REPO_CONFIG, max_bytes=_SMALL_FILE).decode("utf-8"), None
    except FsError as exc:
        return None, exc
    except UnicodeDecodeError:
        return None, FsError("UNREADABLE", "file is not UTF-8")


# -- command ---------------------------------------------------------------------------------------


class CommandError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ArchonCommand:
    """A command a future dispatch would run. Data only: this class cannot run anything."""

    argv: tuple[str, ...]
    cwd: str
    environment: tuple[tuple[str, str], ...]

    def display(self) -> str:
        """Shell-quoted for reading. Never passed to a shell by Factory Core."""
        env = " ".join(f"{k}={shlex.quote(v)}" for k, v in self.environment)
        return f"cd {shlex.quote(self.cwd)} && {env} {shlex.join(self.argv)}"

    def to_dict(self) -> dict[str, Any]:
        document = {"argv": list(self.argv), "cwd": self.cwd, "environment": dict(self.environment)}
        return {**document, "digest": hashlib.sha256(json.dumps(document, sort_keys=True).encode()).hexdigest()}


def _safe(value: str, what: str) -> None:
    bad = unsafe_text(value)
    if bad is not None:
        raise CommandError("UNSAFE_TEXT", f"{what} contains control or formatting character {bad!r}")


def prompt(task: Task) -> str:
    """The run's message: fixed structure, built only from the approved task file's fields."""
    lines = [
        f"Task {task.id} revision {task.revision}: {task.title}",
        "",
        f"Task file: {task.file}",
        f"Specification: {task.spec_path} (sha256 {task.spec_sha256})",
        "Acceptance criteria:",
        *(f"- {item}" for item in task.acceptance),
    ]
    return "\n".join(lines)


def build_command(task: Task, repo: Repository, branch: str, workflow: str, checkout: str) -> ArchonCommand:
    """Build the argument vector. Re-validates every value itself; raises ``CommandError``.

    ``branch`` is the feature branch derived from the task id and revision. ``checkout`` is the
    absolute path of the repository's registered local checkout.
    """
    if not is_task_id(task.id):
        raise CommandError("TASK_ID_UNSAFE", f"task id {task.id!r} is not a plain identifier")
    if not is_command_branch(branch) or not branch.startswith(repo.feature_prefix):
        raise CommandError("FEATURE_BRANCH_UNSAFE", f"feature branch {branch!r} is not a safe branch name")
    if not is_command_branch(repo.integration_branch):
        raise CommandError(
            "INTEGRATION_BRANCH_UNSAFE", f"integration branch {repo.integration_branch!r} is not a safe branch name"
        )
    if branch in (repo.integration_branch, repo.release_branch) or repo.integration_branch == repo.release_branch:
        raise CommandError("BRANCH_CONFLICT", "feature, integration and release branches must all differ")
    if not is_name(workflow) or denied_by_factory(workflow) or workflow not in SUPPORTED_WORKFLOWS:
        raise CommandError("WORKFLOW_FORBIDDEN", f"workflow {workflow!r} may not be run")
    if not checkout.startswith("/") or "\n" in checkout:
        raise CommandError("CHECKOUT_PATH_UNSAFE", "the checkout path must be absolute and on one line")
    _safe(checkout, "checkout path")
    message = prompt(task)
    _safe(message, "task text")
    if len(message.encode("utf-8")) > MAX_PROMPT_BYTES:
        raise CommandError("PROMPT_TOO_LARGE", f"the run message is larger than {MAX_PROMPT_BYTES} bytes")
    argv = (EXECUTABLE, "workflow", "run", workflow, "--branch", branch, "--base", repo.integration_branch, message)
    return ArchonCommand(argv, checkout, ENVIRONMENT)
