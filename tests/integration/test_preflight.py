"""`factory preflight` and `factory reconcile`: end to end, adversarial inputs and the authorization boundary."""

from __future__ import annotations

import dataclasses
import fcntl
import hashlib
import io
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from stage5_support import (
    DISPATCH,
    PROJECT_ID,
    api,
    gh_pull,
    intent,
    make_archon_home,
    make_checkout,
    preflight_responses,
    ready_snapshot,
    remote_responses,
    write_record,
)
from support import copy_project, write_snapshot

from factory.cli import run
from factory.evidence import parse_snapshot
from factory.planning import evaluate
from factory.preflight import authorization, preflight
from factory.project import load_project
from factory.reconcile import State


@dataclasses.dataclass
class Env:
    project: Path
    checkout: Path
    state: Path
    archon: Path
    snapshot: Path

    def preflight_args(self, *extra: str | Path, task: str = "APP-1", revision: int = 3) -> list[str | Path]:
        return [
            "preflight",
            self.project,
            "--task",
            task,
            "--revision",
            str(revision),
            "--checkout",
            self.checkout,
            "--evidence",
            self.snapshot,
            "--state-dir",
            self.state,
            "--archon-home",
            self.archon,
            *extra,
        ]


@pytest.fixture
def env(tmp_path: Path) -> Env:
    project = copy_project(DISPATCH, tmp_path)
    return Env(
        project=project,
        checkout=make_checkout(tmp_path / "checkout"),
        state=tmp_path / "state",
        archon=make_archon_home(tmp_path / "archon"),
        snapshot=write_snapshot(ready_snapshot(project), tmp_path / "snapshot.json"),
    )


def _cli(*args: str | Path, transport: Any = None) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = run([str(a) for a in args], out=out, err=err, transport=transport)
    return code, out.getvalue(), err.getvalue()


def _json(*args: str | Path, transport: Any = None) -> tuple[int, dict[str, Any]]:
    code, out, err = _cli(*args, "--format", "json", transport=transport)
    assert out, err
    return code, json.loads(out)


def _codes(document: dict[str, Any]) -> set[str]:
    return set(document["dryRun"]["reasonCodes"])


def _live_preflight(env: Env, responses: dict[str, Any] | None = None, task: str = "APP-1", revision: int = 3):  # type: ignore[no-untyped-def]
    project = load_project(env.project).project
    assert project is not None
    snapshot = parse_snapshot(json.loads(env.snapshot.read_text()), project, "s").snapshot
    assert snapshot is not None
    github, transport = api(responses if responses is not None else preflight_responses(task, revision))
    report = preflight(
        project, task, revision, env.checkout, env.state, env.archon, evaluate(project, snapshot), github
    )
    return report, transport


# -- valid preflight and the authorization boundary -------------------------------------------------


def test_valid_preflight_passes_the_dry_run_and_is_never_authorized(env: Env) -> None:
    report, transport = _live_preflight(env)
    document = report.to_dict()
    assert document["dryRun"]["result"] == "PASS", document["dryRun"]
    assert {c["code"] for c in document["dryRun"]["checks"]} == {"TASK_READY", "COMMAND_BUILT"}
    assert document["reconciliation"]["state"] == "NO_RUN"
    assert document["authorization"]["granted"] is False
    reasons = {r["code"] for r in document["authorization"]["reasons"]}
    assert {
        "EXECUTION_NOT_IMPLEMENTED",
        "DISPATCH_DISABLED",
        "EVIDENCE_NOT_AUTHENTICATED",
        "EVIDENCE_NOT_COLLECTED",
        "HUMAN_AUTHORIZATION_UNVERIFIABLE",
        "ACTION_TIME_REVALIDATION_MISSING",
        "EXECUTION_LEDGER_MISSING",
    } <= reasons
    assert document["command"]["executed"] is False
    assert document["command"]["argv"][:8] == [
        "archon", "workflow", "run", "archon-implement", "--branch", "work/APP-1-r3", "--base", "next",
    ]  # fmt: skip
    assert document["command"]["cwd"] == str(env.checkout)
    assert document["evidence"] == {"authenticated": False, "provenance": "simulated"}
    assert all(r.startswith("repos/example-org/example-app/") for r in transport.requests)


def test_simulated_ready_never_becomes_authorization(env: Env) -> None:
    """Trust laundering regression: simulated evidence -> READY -> dispatch authority must be impossible."""
    report, _ = _live_preflight(env)
    assert report.plan is not None
    assert next(t for t in report.plan.tasks if t.task.id == "APP-1").state.value == "READY"
    assert report.result.value == "PASS"
    assert report.authorization.granted is False


def test_authorization_is_refused_for_every_evidence_kind_and_even_with_dispatch_enabled(env: Env) -> None:
    project = load_project(env.project).project
    assert project is not None
    enabled = dataclasses.replace(
        project,
        manifest=dataclasses.replace(
            project.manifest, dispatch=dataclasses.replace(project.manifest.dispatch, enabled=True)
        ),
    )
    for provenance in ("simulated", "github", "authenticated", "human"):
        document = {**ready_snapshot(env.project), "provenance": {"kind": provenance}}
        snapshot = parse_snapshot(document, project, "s").snapshot
        plan = evaluate(project, snapshot) if snapshot is not None else None
        for candidate in (project, enabled):
            result = authorization(candidate, plan)
            assert result.granted is False
            assert "EXECUTION_NOT_IMPLEMENTED" in {r.code for r in result.reasons}
    assert authorization(enabled, None).granted is False


def test_dispatch_enabled_true_is_still_rejected_by_validation(env: Env) -> None:
    manifest = env.project / "factory/project.yaml"
    document = yaml.safe_load(manifest.read_text())
    document["dispatch"]["enabled"] = True
    manifest.write_text(yaml.safe_dump(document, sort_keys=False))
    code, out, err = _cli(*env.preflight_args())
    assert code == 2
    assert "DISPATCH_NOT_SUPPORTED" in err


@pytest.mark.parametrize("flag", ["--execute", "--run", "--yes", "--force", "--dispatch", "--adopt", "--abandon"])
def test_there_is_no_execution_or_mutation_option(env: Env, flag: str) -> None:
    assert _cli(*env.preflight_args(flag))[0] == 64
    assert _cli("reconcile", env.project, "--task", "APP-1", "--offline", flag)[0] == 64


@pytest.mark.parametrize("command", ["dispatch", "execute", "run"])
def test_there_is_no_dispatch_command(command: str) -> None:
    assert _cli(command, DISPATCH)[0] == 64


# -- offline CLI ---------------------------------------------------------------------------------


def test_offline_preflight_reports_unchecked_remote_facts_and_refuses(env: Env) -> None:
    code, document = _json(*env.preflight_args())
    assert code == 1
    assert {"ARCHON_REMOTE_CONFIG_UNCHECKED", "REMOTE_BRANCH_UNCHECKED", "RECONCILE_UNKNOWN"} <= _codes(document)
    assert document["dryRun"]["result"] == "UNKNOWN"
    assert document["authorization"]["granted"] is False
    assert document["command"] is not None


def test_text_output_states_that_nothing_runs(env: Env) -> None:
    code, out, _ = _cli(*env.preflight_args())
    assert code == 1
    assert "Preflight (dry run, nothing executed)" in out
    assert "Authorization: NOT GRANTED" in out
    assert "Command (display only; never executed by this version):" in out
    assert "cd " in out and "DO_NOT_TRACK=1 archon workflow run archon-implement --branch work/APP-1-r3" in out


def test_output_is_byte_identical_for_identical_inputs(env: Env) -> None:
    for fmt in ("json", "text"):
        first = _cli(*env.preflight_args("--format", fmt))
        second = _cli(*env.preflight_args("--format", fmt))
        assert first == second
    first_report, _ = _live_preflight(env)
    second_report, _ = _live_preflight(env)
    assert json.dumps(first_report.to_dict(), sort_keys=True) == json.dumps(second_report.to_dict(), sort_keys=True)


def test_inputs_are_bound_to_the_report(env: Env) -> None:
    _, document = _json(*env.preflight_args())
    project = load_project(env.project).project
    assert project is not None
    assert document["inputs"] == {
        "manifestSha256": project.digests["factory/project.yaml"],
        "taskSha256": project.digests["factory/tasks/APP-1.yaml"],
    }
    assert len(document["command"]["digest"]) == 64


# -- preflight refusals -------------------------------------------------------------------------


def test_revision_mismatch(env: Env) -> None:
    _, document = _json(*env.preflight_args(revision=2))
    assert "TASK_REVISION_MISMATCH" in _codes(document)
    assert document["command"] is None  # never a command for a revision the caller did not ask about


def test_unknown_task(env: Env) -> None:
    code, document = _json(*env.preflight_args(task="APP-9"))
    assert code == 1
    assert _codes(document) == {"TASK_NOT_FOUND"}
    assert document["command"] is None


@pytest.mark.parametrize(
    ("version", "code"),
    [("0.15.0", "ARCHON_VERSION_MISMATCH"), (None, "ARCHON_VERSION_UNKNOWN"), ("garbage", "ARCHON_VERSION_UNKNOWN")],
)
def test_archon_version_must_match(env: Env, version: str | None, code: str) -> None:
    make_archon_home(env.archon, version=version)
    if version is None:
        (env.archon / "install.json").unlink()
    _, document = _json(*env.preflight_args())
    assert code in _codes(document)


def test_missing_archon_home(env: Env, tmp_path: Path) -> None:
    _, document = _json(*env.preflight_args()[:-1], tmp_path / "no-archon")
    assert "ARCHON_HOME_UNREADABLE" in _codes(document)


def test_task_provider_not_archons_default_is_refused(env: Env) -> None:
    report, _ = _live_preflight(env, preflight_responses("APP-2", 1), task="APP-2", revision=1)
    assert "PROVIDER_NOT_ARCHON_DEFAULT" in report.to_dict()["dryRun"]["reasonCodes"]


def test_denied_workflow_in_manifest_is_refused(env: Env) -> None:
    manifest = env.project / "factory/project.yaml"
    document = yaml.safe_load(manifest.read_text())
    document["dispatch"]["workflows"] = {"default": "archon-merge-queue", "allowed": ["archon-merge-queue"]}
    manifest.write_text(yaml.safe_dump(document, sort_keys=False))
    code, out, err = _cli(*env.preflight_args())
    assert code == 2
    assert "WORKFLOW_FORBIDDEN" in err
    code, _, err = _cli("validate", env.project)
    assert code == 2 and "WORKFLOW_FORBIDDEN" in err


def test_task_workflow_not_allowed_is_a_validation_error(env: Env) -> None:
    task = env.project / "factory/tasks/APP-1.yaml"
    task.write_text(task.read_text() + "execution: {workflow: archon-build-queue}\n")
    code, _, err = _cli("validate", env.project)
    assert code == 2 and "WORKFLOW_NOT_ALLOWED" in err


@pytest.mark.parametrize(
    ("checkout", "code"),
    [
        ({"archon_base": None}, "ARCHON_REPO_CONFIG_MISSING"),
        ({"archon_base": "stable"}, "ARCHON_BASE_BRANCH_MISMATCH"),
        ({"archon_text": "worktree: {"}, "ARCHON_REPO_CONFIG_INVALID"),
        ({"head": "work/APP-1-r3"}, "CHECKOUT_ON_FEATURE_BRANCH"),
        ({"branches": {"work/APP-1-r3": "a" * 40}}, "FEATURE_BRANCH_EXISTS_LOCAL"),
        ({"worktrees": {"x": "work/APP-1-r3"}}, "FEATURE_WORKTREE_EXISTS"),
        ({"remotes": {"work/APP-1-r3": "a" * 40}}, "REMOTE_TRACKING_BRANCH_EXISTS"),
        ({"origin": "https://github.com/example-org/other.git"}, "CHECKOUT_REPOSITORY_MISMATCH"),
    ],
)
def test_checkout_state_refusals(env: Env, tmp_path: Path, checkout: dict[str, Any], code: str) -> None:
    env.checkout = make_checkout(tmp_path / "other-checkout", **checkout)
    _, document = _json(*env.preflight_args())
    assert code in _codes(document)


def test_remote_state_refusals(env: Env) -> None:
    report, _ = _live_preflight(env, preflight_responses(remote_config=None))
    assert "ARCHON_REMOTE_REPO_CONFIG_MISSING" in report.to_dict()["dryRun"]["reasonCodes"]
    report, _ = _live_preflight(env, preflight_responses(remote_config="worktree:\n  baseBranch: stable\n"))
    assert "ARCHON_REMOTE_BASE_BRANCH_MISMATCH" in report.to_dict()["dryRun"]["reasonCodes"]
    report, _ = _live_preflight(env, preflight_responses(remote_branches=["work/APP-1-r3"]))
    codes = set(report.to_dict()["dryRun"]["reasonCodes"])
    assert {"FEATURE_BRANCH_EXISTS_REMOTE", "RECONCILE_UNTRACKED_BRANCH"} <= codes


def test_superseded_pr_blocks_the_dry_run(env: Env) -> None:
    responses = preflight_responses(pulls={2: [gh_pull(5, "work/APP-1-r2")]})
    report, _ = _live_preflight(env, responses)
    assert "RECONCILE_SUPERSEDED_PR_OPEN" in report.to_dict()["dryRun"]["reasonCodes"]


def test_interrupted_dispatch_blocks_the_dry_run(env: Env) -> None:
    write_record(env.state, intent())
    report, _ = _live_preflight(env)
    assert "RECONCILE_UNCERTAIN" in report.to_dict()["dryRun"]["reasonCodes"]


def test_task_not_ready_is_refused(env: Env) -> None:
    snapshot = ready_snapshot(env.project)
    snapshot["approvals"] = []
    write_snapshot(snapshot, env.snapshot)
    _, document = _json(*env.preflight_args())
    assert "TASK_NOT_READY" in _codes(document)


def test_lock_held_exits_3(env: Env) -> None:
    lock = env.state / PROJECT_ID / "dispatch.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("")
    fd = os.open(lock, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        code, document = _json(*env.preflight_args())
        assert code == 3
        assert "RECONCILE_COORDINATOR_ACTIVE" in _codes(document)
        code, _, _ = _cli("reconcile", env.project, "--task", "APP-1", "--offline", "--state-dir", env.state)
        assert code == 3
        # The refusal is immediate: no GitHub request, no command.
        report, transport = _live_preflight(env, responses={})
        assert transport.requests == []
        assert report.command is None and report.lock_held
        assert [c.code for c in report.checks] == ["RECONCILE_COORDINATOR_ACTIVE"]
    finally:
        os.close(fd)


def test_input_change_during_preflight_is_detected(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    from factory import preflight as module

    original = module.decide

    def tamper(obs):  # type: ignore[no-untyped-def]
        task = env.project / "factory/tasks/APP-1.yaml"
        task.write_text(task.read_text().replace("Export items as CSV", "Export everything"))
        return original(obs)

    monkeypatch.setattr(module, "decide", tamper)
    _, document = _json(*env.preflight_args())
    assert "INPUT_CHANGED_DURING_PREFLIGHT" in _codes(document)


# -- injection and unsafe values from untrusted files ------------------------------------------------


def test_shell_metacharacters_in_task_text_stay_data(env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    marker = Path("pwned")
    task = env.project / "factory/tasks/APP-1.yaml"
    document = yaml.safe_load(task.read_text())
    document["title"] = f"$(touch {marker}); `touch {marker}` && touch {marker} | x"
    document["acceptance"] = ["--base stable", "-b main --detach"]
    task.write_text(yaml.safe_dump(document, sort_keys=False))
    write_snapshot(ready_snapshot(env.project), env.snapshot)
    _, document = _json(*env.preflight_args())
    argv = document["command"]["argv"]
    assert len(argv) == 9 and argv.count("--base") == 1 and "--detach" not in argv[:8]
    assert argv[8].startswith(f"Task APP-1 revision 3: $(touch {marker})")
    assert not marker.exists()


def test_control_characters_in_task_text_are_refused(env: Env) -> None:
    task = env.project / "factory/tasks/APP-1.yaml"
    document = yaml.safe_load(task.read_text())
    document["title"] = "Export \x1b[2J items"
    task.write_text(yaml.safe_dump(document, sort_keys=False))
    write_snapshot(ready_snapshot(env.project), env.snapshot)
    code, document = _json(*env.preflight_args())
    assert code == 1
    assert {"TASK_TEXT_UNSAFE", "UNSAFE_TEXT"} <= _codes(document)
    assert document["command"] is None


def test_trailing_newline_values_that_pass_the_schema_are_refused(env: Env) -> None:
    """JSON Schema patterns use search semantics, so '$' accepts a trailing newline (debt item 4)."""
    manifest = env.project / "factory/project.yaml"
    document = yaml.safe_load(manifest.read_text())
    document["repositories"][0]["branches"]["integration"] = "next\n"
    manifest.write_text(yaml.safe_dump(document, sort_keys=False))
    assert _cli("validate", env.project)[0] == 0  # the schema alone lets it through
    write_snapshot(ready_snapshot(env.project), env.snapshot)
    code, result = _json(*env.preflight_args())
    assert code == 1
    assert "INTEGRATION_BRANCH_UNSAFE" in _codes(result)
    assert result["command"] is None


def test_task_id_with_trailing_newline_builds_no_command_and_reads_no_records(env: Env) -> None:
    task = env.project / "factory/tasks/APP-1.yaml"
    document = yaml.safe_load(task.read_text())
    document["id"] = "APP-1\n"
    task.write_text(yaml.safe_dump(document, sort_keys=False))
    write_snapshot(ready_snapshot(env.project), env.snapshot)
    assert _cli("validate", env.project)[0] == 0  # the schema alone lets it through
    code, result = _json(*env.preflight_args(task="APP-1\n"))
    assert code == 1
    assert {"FEATURE_BRANCH_UNSAFE", "TASK_ID_UNSAFE"} <= _codes(result)
    assert result["command"] is None
    assert "RECORD_PATH_UNSAFE" in {f["code"] for f in result["reconciliation"]["findings"]}


def test_checkout_path_with_control_characters_builds_no_command(env: Env, tmp_path: Path) -> None:
    odd = tmp_path / "check\x1bout"
    make_checkout(odd)
    env.checkout = odd
    _, document = _json(*env.preflight_args())
    assert document["command"] is None
    assert "UNSAFE_TEXT" in _codes(document)


# -- side effects -------------------------------------------------------------------------------------


def _fingerprint(*roots: Path) -> dict[str, Any]:
    found: dict[str, Any] = {}
    for root in roots:
        for path in sorted(root.rglob("*")):
            info = path.lstat()
            entry: Any = (info.st_mode, info.st_mtime_ns, info.st_size)
            if path.is_file() and not path.is_symlink():
                entry = (*entry, hashlib.sha256(path.read_bytes()).hexdigest())
            found[str(path)] = entry
    return found


def test_preflight_and_reconcile_write_nothing(env: Env) -> None:
    write_record(env.state, intent())
    roots = (env.project, env.checkout, env.state, env.archon)
    before = _fingerprint(*roots)
    _cli(*env.preflight_args())
    _live_preflight(env)
    _cli("reconcile", env.project, "--task", "APP-1", "--checkout", env.checkout, "--offline", "--state-dir", env.state)
    assert _fingerprint(*roots) == before


def test_no_process_is_started(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[Any] = []

    def refuse(*args: Any, **kwargs: Any) -> Any:
        started.append(args)
        raise AssertionError(f"process start attempted: {args!r}")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(subprocess, "run", refuse)
    for name in ("system", "popen", "fork", "forkpty", "posix_spawn", "posix_spawnp", "spawnv", "spawnve", "execv", "execve", "execvp", "execvpe"):  # fmt: skip
        if hasattr(os, name):
            monkeypatch.setattr(os, name, refuse)
    code, _ = _json(*env.preflight_args())
    assert code == 1
    _live_preflight(env)
    _cli("reconcile", env.project, "--task", "APP-1", "--checkout", env.checkout, "--offline", "--state-dir", env.state)
    _cli(
        "reconcile", env.project, "--task", "APP-1", "--checkout", env.checkout, "--live", "--state-dir", env.state,
        transport=api(remote_responses())[1],
    )  # fmt: skip
    assert started == []


# -- reconcile CLI ------------------------------------------------------------------------------------


def test_reconcile_cli_live(env: Env) -> None:
    transport = api(remote_responses(remote_branches=["work/APP-1-r3"]))[1]
    args = ["reconcile", env.project, "--task", "APP-1", "--checkout", env.checkout, "--live", "--state-dir", env.state]
    code, document = _json(*args, transport=transport)
    assert code == 0
    assert document["schemaVersion"] == "factory.reconcile/v1"
    assert document["state"] == State.UNTRACKED_BRANCH.value
    assert document["blocksDispatch"] is True
    transport2 = api(remote_responses(remote_branches=["work/APP-1-r3"]))[1]
    assert (
        _cli(*args, "--format", "json", transport=transport2)[1]
        == _cli(*args, "--format", "json", transport=api(remote_responses(remote_branches=["work/APP-1-r3"]))[1])[1]
    )


def test_reconcile_cli_offline_is_incomplete(env: Env) -> None:
    code, out, _ = _cli(
        "reconcile", env.project, "--task", "APP-1", "--checkout", env.checkout, "--offline", "--state-dir", env.state
    )
    assert code == 3
    assert "State: UNKNOWN" in out and "REMOTE_NOT_READ" in out


def test_reconcile_cli_requires_a_source_and_a_known_task(env: Env) -> None:
    assert _cli("reconcile", env.project, "--task", "APP-1")[0] == 64
    assert _cli("reconcile", env.project, "--task", "APP-1", "--live", "--offline")[0] == 64
    assert _cli("reconcile", env.project, "--task", "NOPE", "--offline")[0] == 64


def test_every_state_but_no_run_blocks_dispatch() -> None:
    """No automatic retry: failed, abandoned and PR-less attempts all need a human decision."""
    from factory.reconcile import Reconciliation

    for state in State:
        blocked = Reconciliation(state, (), (), 0, 2).blocks_dispatch
        assert blocked is (state is not State.NO_RUN), state
