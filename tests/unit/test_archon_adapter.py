"""Archon adapter: workflow and provider policy, Archon state readers and the command builder."""

from __future__ import annotations

import dataclasses
import json
import shlex
from pathlib import Path

import pytest
from stage5_support import DISPATCH, make_archon_home

from factory import archon
from factory.archon import CommandError, build_command
from factory.model import Dispatch, Task
from factory.project import load_project


def _project():  # type: ignore[no-untyped-def]
    project = load_project(DISPATCH).project
    assert project is not None
    return project


def _task(**changes) -> Task:  # type: ignore[no-untyped-def]
    return dataclasses.replace(_project().tasks["APP-1"], **changes)


def _repo():  # type: ignore[no-untyped-def]
    repo = _project().manifest.repository("app")
    assert repo is not None
    return repo


def _dispatch(**changes) -> Dispatch:  # type: ignore[no-untyped-def]
    return dataclasses.replace(_project().manifest.dispatch, **changes)


def _codes(findings: list[archon.Finding]) -> set[str]:
    return {f.code for f in findings}


# -- workflow policy -------------------------------------------------------------------------------


def test_default_workflow_resolves() -> None:
    assert archon.resolve_workflow(_task(), _dispatch()) == ("archon-implement", [])


@pytest.mark.parametrize(
    "workflow",
    [
        "archon-merge-queue",
        "archon-build-queue",
        "archon-intake",
        "archon-worktree-cleanup",
        "my-merge-helper",
        "nightly.deploy",
        "release_notes",
        "auto-ship",
        "queue",
    ],
)
def test_denied_workflows_are_refused_even_when_the_manifest_allows_them(workflow: str) -> None:
    dispatch = _dispatch(allowed_workflows=(workflow,), default_workflow=workflow)
    resolved, findings = archon.resolve_workflow(_task(), dispatch)
    assert resolved is None
    assert "WORKFLOW_FORBIDDEN" in _codes(findings)


def test_a_workflow_must_be_allowed_supported_and_not_forbidden_by_the_manifest() -> None:
    _, findings = archon.resolve_workflow(_task(workflow="archon-plan"), _dispatch())
    assert _codes(findings) == {"WORKFLOW_NOT_ALLOWED", "WORKFLOW_NOT_SUPPORTED"}
    _, findings = archon.resolve_workflow(_task(workflow="archon-plan"), _dispatch(allowed_workflows=("archon-plan",)))
    assert _codes(findings) == {"WORKFLOW_NOT_SUPPORTED"}
    _, findings = archon.resolve_workflow(_task(), _dispatch(forbidden_workflows=("archon-implement",)))
    assert _codes(findings) == {"WORKFLOW_FORBIDDEN"}


@pytest.mark.parametrize("workflow", ["--detach", "archon-implement --no-worktree", "../x", "Archon-Implement", ""])
def test_malformed_workflow_names_are_refused(workflow: str) -> None:
    resolved, findings = archon.resolve_workflow(_task(workflow=workflow), _dispatch())
    assert resolved is None
    assert _codes(findings) == {"WORKFLOW_INVALID"} or _codes(findings) == {"WORKFLOW_NOT_CONFIGURED"}


def test_missing_workflow_configuration() -> None:
    _, findings = archon.resolve_workflow(_task(), _dispatch(default_workflow=None))
    assert _codes(findings) == {"WORKFLOW_NOT_CONFIGURED"}


# -- providers and Archon state --------------------------------------------------------------------


def test_provider_resolution() -> None:
    assert archon.resolve_provider(_task(), _dispatch()) == ("claude", "claude", [])
    assert archon.resolve_provider(_task(provider="codex"), _dispatch()) == ("codex", "codex", [])
    assert _codes(archon.resolve_provider(_task(provider="gemini"), _dispatch())[2]) == {"PROVIDER_NOT_AVAILABLE"}
    assert _codes(archon.resolve_provider(_task(), _dispatch(default_provider=None))[2]) == {"PROVIDER_NOT_CONFIGURED"}
    bad = _dispatch(providers={"claude": "--model=x"})
    assert _codes(archon.resolve_provider(_task(), bad)[2]) == {"PROVIDER_ASSISTANT_INVALID"}


def test_archon_home_is_read_from_files(tmp_path: Path) -> None:
    install = archon.read_archon_home(make_archon_home(tmp_path))
    assert (install.version, install.default_assistant) == ("0.14.0", "claude")
    assert install.version_problem is None and install.assistant_problem is None


@pytest.mark.parametrize(
    ("content", "code"),
    [
        (None, "ARCHON_VERSION_UNKNOWN"),
        ("{not json", "ARCHON_VERSION_UNKNOWN"),
        ('{"version": "0.14.0\\n"}', "ARCHON_VERSION_UNKNOWN"),
        ('{"version": 14}', "ARCHON_VERSION_UNKNOWN"),
        ('{"version": "0.14.0", "version": "9.9.9"}', "ARCHON_VERSION_UNKNOWN"),
    ],
)
def test_unreadable_install_records_are_unknown(tmp_path: Path, content: str | None, code: str) -> None:
    home = make_archon_home(tmp_path, version=None)
    if content is not None:
        (home / "install.json").write_text(content)
    install = archon.read_archon_home(home)
    assert install.version is None
    assert install.version_problem is not None and install.version_problem.code == code
    assert install.version_problem.result == "UNKNOWN"


def test_symlinked_install_record_is_not_followed(tmp_path: Path) -> None:
    other = tmp_path / "other.json"
    other.write_text(json.dumps({"version": "0.14.0"}))
    home = make_archon_home(tmp_path / "home", version=None)
    (home / "install.json").symlink_to(other)
    install = archon.read_archon_home(home)
    assert install.version is None and install.version_problem is not None


def test_version_checks() -> None:
    dispatch = _dispatch()
    assert archon.check_archon_version(dispatch, "0.14.0", None) == []
    assert _codes(archon.check_archon_version(dispatch, "0.15.0", None)) == {"ARCHON_VERSION_MISMATCH"}
    assert _codes(archon.check_archon_version(_dispatch(archon_version=None), "0.14.0", None)) == {
        "ARCHON_VERSION_NOT_CONFIGURED"
    }
    pinned_elsewhere = _dispatch(archon_version="0.15.0")
    assert _codes(archon.check_archon_version(pinned_elsewhere, "0.15.0", None)) == {"ARCHON_VERSION_UNSUPPORTED"}
    problem = archon.Finding("ARCHON_VERSION_UNKNOWN", "UNKNOWN", "missing")
    assert archon.check_archon_version(dispatch, None, problem) == [problem]


def test_provider_must_be_archons_default_assistant(tmp_path: Path) -> None:
    install = archon.read_archon_home(make_archon_home(tmp_path))
    assert archon.check_assistant("claude", install) == []
    assert _codes(archon.check_assistant("codex", install)) == {"PROVIDER_NOT_ARCHON_DEFAULT"}
    unknown = archon.read_archon_home(make_archon_home(tmp_path / "x", assistant=None))
    assert _codes(archon.check_assistant("claude", unknown)) == {"ARCHON_DEFAULT_ASSISTANT_UNKNOWN"}


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("worktree:\n  baseBranch: next\n", None),
        ("worktree:\n  baseBranch: stable\n", "ARCHON_BASE_BRANCH_MISMATCH"),
        ("worktree:\n  copyFiles: []\n", "ARCHON_BASE_BRANCH_UNSET"),
        ("worktree: [", "ARCHON_REPO_CONFIG_INVALID"),
        ("worktree:\n  baseBranch: next\n  baseBranch: stable\n", "ARCHON_REPO_CONFIG_INVALID"),
        ("[]", "ARCHON_BASE_BRANCH_UNSET"),
    ],
)
def test_repo_config_checks(text: str, code: str | None) -> None:
    findings = archon.check_repo_config(text, None, "next", "local")
    assert _codes(findings) == ({code} if code else set())
    remote = archon.check_repo_config(text, None, "next", "remote")
    assert _codes(remote) == ({code.replace("ARCHON_", "ARCHON_REMOTE_", 1)} if code else set())


def test_repo_config_files_are_read_without_following_links(tmp_path: Path) -> None:
    checkout = tmp_path / "c"
    (checkout / ".archon").mkdir(parents=True)
    target = tmp_path / "evil.yaml"
    target.write_text("worktree:\n  baseBranch: next\n")
    (checkout / ".archon" / "config.yaml").symlink_to(target)
    text, problem = archon.read_local_repo_config(checkout)
    assert text is None and problem is not None
    assert _codes(archon.check_repo_config(text, problem, "next", "local")) == {"ARCHON_REPO_CONFIG_UNSAFE"}
    text, problem = archon.read_local_repo_config(tmp_path / "missing")
    assert _codes(archon.check_repo_config(text, problem, "next", "local")) == {"ARCHON_REPO_CONFIG_MISSING"}


# -- command builder --------------------------------------------------------------------------------


def test_command_is_a_structured_argument_vector() -> None:
    command = build_command(_task(), _repo(), "work/APP-1-r3", "archon-implement", "/src/example-app")
    assert command.argv[:8] == (
        "archon",
        "workflow",
        "run",
        "archon-implement",
        "--branch",
        "work/APP-1-r3",
        "--base",
        "next",
    )
    assert command.argv[8].startswith("Task APP-1 revision 3: Export items as CSV\n")
    assert len(command.argv) == 9
    assert command.cwd == "/src/example-app"
    assert command.environment == (("DO_NOT_TRACK", "1"),)
    options = {a for a in command.argv if a.startswith("-")}
    assert options <= archon.EMITTED_OPTIONS
    for forbidden in ("--detach", "--no-worktree", "--folder", "--adopt", "--resume", "--model", "--config", "--input"):
        assert forbidden not in command.argv


def test_identical_inputs_give_identical_commands() -> None:
    first = build_command(_task(), _repo(), "work/APP-1-r3", "archon-implement", "/src/example-app")
    second = build_command(_task(), _repo(), "work/APP-1-r3", "archon-implement", "/src/example-app")
    assert first == second
    assert json.dumps(first.to_dict(), sort_keys=True) == json.dumps(second.to_dict(), sort_keys=True)
    assert first.display() == second.display()


def test_display_quotes_for_reading_and_round_trips() -> None:
    task = _task(title="Use $(rm -rf ~) and `id`; echo 'x' > /tmp/y")
    command = build_command(task, _repo(), "work/APP-1-r3", "archon-implement", "/src/my app")
    display = command.display()
    assert display.startswith("cd '/src/my app' && DO_NOT_TRACK=1 archon workflow run")
    # The displayed command line parses back to exactly the argument vector: nothing is interpreted.
    assert shlex.split(display.split(" && ", 1)[1])[1:] == list(command.argv)


def test_task_text_cannot_add_arguments() -> None:
    task = _task(title="x --no-worktree --detach", acceptance=("--base stable", "-b main"))
    command = build_command(task, _repo(), "work/APP-1-r3", "archon-implement", "/src/example-app")
    assert len(command.argv) == 9
    assert command.argv.count("--base") == 1 and "--detach" not in command.argv[:8]


@pytest.mark.parametrize("title", ["\x1b]0;evil\x07", "a\rb", "x\u202ey", "nul\x00"])
def test_control_characters_in_task_text_are_refused(title: str) -> None:
    with pytest.raises(CommandError) as info:
        build_command(_task(title=title), _repo(), "work/APP-1-r3", "archon-implement", "/src/example-app")
    assert info.value.code == "UNSAFE_TEXT"


@pytest.mark.parametrize(
    ("branch", "repo_changes", "workflow", "checkout", "code"),
    [
        ("work/APP-1-r3\n", {}, "archon-implement", "/c", "FEATURE_BRANCH_UNSAFE"),
        ("-work/APP-1-r3", {}, "archon-implement", "/c", "FEATURE_BRANCH_UNSAFE"),
        ("other/APP-1-r3", {}, "archon-implement", "/c", "FEATURE_BRANCH_UNSAFE"),
        ("work/APP-1-r3", {"integration_branch": "-x"}, "archon-implement", "/c", "INTEGRATION_BRANCH_UNSAFE"),
        ("work/APP-1-r3", {"integration_branch": "next\n"}, "archon-implement", "/c", "INTEGRATION_BRANCH_UNSAFE"),
        ("work/APP-1-r3", {"integration_branch": "stable"}, "archon-implement", "/c", "BRANCH_CONFLICT"),
        ("work/APP-1-r3", {}, "archon-merge-queue", "/c", "WORKFLOW_FORBIDDEN"),
        ("work/APP-1-r3", {}, "archon-plan", "/c", "WORKFLOW_FORBIDDEN"),
        ("work/APP-1-r3", {}, "archon-implement", "relative/path", "CHECKOUT_PATH_UNSAFE"),
        ("work/APP-1-r3", {}, "archon-implement", "/c\n/d", "CHECKOUT_PATH_UNSAFE"),
        ("work/APP-1-r3", {}, "archon-implement", "/c\x1b[2J", "UNSAFE_TEXT"),
    ],
)
def test_builder_revalidates_every_value(  # type: ignore[no-untyped-def]
    branch: str, repo_changes, workflow: str, checkout: str, code: str
) -> None:
    repo = dataclasses.replace(_repo(), **repo_changes)
    with pytest.raises(CommandError) as info:
        build_command(_task(), repo, branch, workflow, checkout)
    assert info.value.code == code


def test_task_id_with_trailing_newline_is_refused() -> None:
    with pytest.raises(CommandError) as info:
        build_command(_task(id="APP-1\n"), _repo(), "work/APP-1-r3", "archon-implement", "/c")
    assert info.value.code == "TASK_ID_UNSAFE"


def test_oversized_prompt_is_refused() -> None:
    task = _task(acceptance=("x" * 20_000,))
    with pytest.raises(CommandError) as info:
        build_command(task, _repo(), "work/APP-1-r3", "archon-implement", "/c")
    assert info.value.code == "PROMPT_TOO_LARGE"


def test_command_contains_no_credentials() -> None:
    command = build_command(_task(), _repo(), "work/APP-1-r3", "archon-implement", "/c")
    text = json.dumps(command.to_dict()) + command.display()
    for marker in ("TOKEN", "GH_", "GITHUB_", "PASSWORD", "SECRET", "ANTHROPIC", "OPENAI"):
        assert marker not in text.upper().replace("DO_NOT_TRACK", "")


def test_command_object_has_no_way_to_run() -> None:
    command = build_command(_task(), _repo(), "work/APP-1-r3", "archon-implement", "/c")
    public = {name for name in dir(command) if not name.startswith("_")}
    assert public == {"argv", "cwd", "environment", "display", "to_dict"}
