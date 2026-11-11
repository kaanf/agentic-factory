"""`factory evidence collect`: recorded real GitHub data, CLI behavior and transport safety."""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from conftest import codes, task_of
from github_support import SELF_PROJECT, FakeTransport, load_recorded
from support import copy_project

from factory import github
from factory.cli import run
from factory.collector import collect
from factory.github import GhCliTransport, GitHubApi, GitHubError, Response
from factory.project import load_project

DEV_HEAD = "4f7b49d6b16b73f3de7c5a384f4ca05736046e6b"  # merge commit of PR #1, with push CI


def _cli(*args: str | Path, transport: Any = None) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = run([str(a) for a in args], out=out, err=err, transport=transport)
    return code, out.getvalue(), err.getvalue()


# -- recorded responses from the real repository --------------------------------


def test_recorded_github_responses_produce_the_expected_snapshot() -> None:
    project = load_project(SELF_PROJECT).project
    assert project is not None
    transport = load_recorded()
    result = collect(project, GitHubApi(transport), now=lambda: datetime(2026, 11, 11, tzinfo=UTC))
    assert result.errors == []
    facts = result.snapshot["repositories"]["factory"]
    assert facts["integrationHead"] == DEV_HEAD
    assert facts["branches"] == {
        "feat/approval-verification-r1": {"pullRequests": []},
        "feat/evidence-collector-r1": {"pullRequests": []},
    }
    assert facts["commits"][DEV_HEAD] == {
        "checkRuns": [
            {
                "app": "github-actions",
                "attempt": 1,
                "conclusion": "success",
                "event": "push",
                "headBranch": "dev",
                "name": "Factory Core Check",
                "status": "completed",
                "workflowPath": ".github/workflows/ci.yml",
            }
        ],
        "mergedPullRequests": [1],
        "reachableFromIntegrationHead": True,
    }
    # Every recorded response was needed, and nothing else was requested.
    assert sorted(set(transport.requests)) == sorted(transport.responses)


def test_recorded_snapshot_plans_with_the_existing_engine(tmp_path: Path) -> None:
    snapshot_path = tmp_path / "live.json"
    code, out, err = _cli("evidence", "collect", SELF_PROJECT, "--output", snapshot_path, transport=load_recorded())
    assert (code, out, err) == (0, "", "")
    code, out, err = _cli("plan", SELF_PROJECT, "--evidence", snapshot_path, "--format", "json")
    assert code == 0, err
    plan = json.loads(out)
    assert plan["evidence"]["provenance"] == "github"
    assert plan["evidence"]["authenticated"] is False
    task = task_of(plan, "evidence-collector")
    # The real integration head passes integration evidence on its own...
    assert task["levels"]["integration-ready"]["reasons"] == []
    # ...but nothing can start: approvals are not collected and stay UNKNOWN.
    assert task["state"] == "UNKNOWN"
    assert "APPROVAL_EVIDENCE_MISSING" in codes(task["gates"]["start"]["reasons"])
    dependent = task_of(plan, "approval-verification")
    assert dependent["state"] == "BLOCKED"
    assert "NOT_MERGED" in codes(dependent["gates"]["start"]["reasons"])


# -- CLI behavior ---------------------------------------------------------------------


def test_collect_writes_json_to_stdout() -> None:
    code, out, err = _cli("evidence", "collect", SELF_PROJECT, transport=load_recorded())
    assert code == 0 and err == ""
    assert json.loads(out)["provenance"]["kind"] == "github"


def test_incomplete_collection_exits_3_but_still_writes_the_snapshot(tmp_path: Path) -> None:
    transport = load_recorded()
    key = next(k for k in transport.responses if "/check-runs" in k)
    transport.responses[key] = Response(500, {"message": "Server Error"})
    output = tmp_path / "s.json"
    code, out, err = _cli("evidence", "collect", SELF_PROJECT, "-o", output, transport=transport)
    assert code == 3
    assert f"incomplete: factory: commits.{DEV_HEAD}.checkRuns: HTTP 500 Server Error" in err
    snapshot = json.loads(output.read_text())
    assert snapshot["collection"]["complete"] is False
    assert "checkRuns" not in snapshot["repositories"]["factory"]["commits"][DEV_HEAD]


def test_output_inside_project_is_refused(tmp_path: Path) -> None:
    project = copy_project(SELF_PROJECT, tmp_path)
    code, _, err = _cli("evidence", "collect", project, "-o", project / "snapshot.json", transport=load_recorded())
    assert code == 64
    assert "must not be inside the project directory" in err
    assert not (project / "snapshot.json").exists()


def test_collect_refuses_an_invalid_project(tmp_path: Path) -> None:
    project = copy_project(SELF_PROJECT, tmp_path)
    (project / "factory/tasks/evidence-collector.yaml").write_text("approvedBy: kaanf\n")
    transport = FakeTransport({})
    code, _, err = _cli("evidence", "collect", project, transport=transport)
    assert code == 2
    assert "SELF_DECLARED_APPROVAL" in err
    assert transport.requests == []


def test_missing_github_cli_exits_3(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(github.shutil, "which", lambda _name: None)
    code, out, err = _cli("evidence", "collect", SELF_PROJECT)
    assert code == 3 and out == ""
    assert "GitHub CLI (gh) is not installed" in err


def test_collect_never_writes_to_the_project(tmp_path: Path) -> None:
    project = copy_project(SELF_PROJECT, tmp_path)

    def fingerprint() -> dict[str, str]:
        return {
            p.as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(project.rglob("*")) if p.is_file()
        }

    before = fingerprint()
    assert _cli("evidence", "collect", project, "-o", tmp_path / "out.json", transport=load_recorded())[0] == 0
    assert fingerprint() == before


# -- transport safety -------------------------------------------------------------------


def test_gh_transport_runs_only_get_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        body = 'HTTP/2.0 200 OK\r\nX-Ratelimit-Remaining: 4999\r\n\r\n{"full_name": "a/b"}'
        return subprocess.CompletedProcess(command, 0, stdout=body, stderr="")

    monkeypatch.setattr(github.subprocess, "run", fake_run)
    response = GhCliTransport(executable="/usr/bin/gh").get("repos/a/b")
    assert response == Response(200, {"full_name": "a/b"}, {"x-ratelimit-remaining": "4999"})
    command = calls[0]
    assert command[1:4] == ["api", "--method", "GET"]
    assert not any(flag in command for flag in ("-f", "-F", "--field", "--raw-field", "--input", "-X"))


@pytest.mark.parametrize(
    "path",
    [
        "user",
        "graphql",
        "https://evil.example/repos/a/b",
        "repos/a/b/../../user",
        "repos/a/b/%2E%2E/%2e%2e/user",
        "repos/a/b/./pulls",
        "repos/a",
        "repos/a/b c",
    ],
)
def test_gh_transport_refuses_paths_outside_repos(path: str) -> None:
    with pytest.raises(GitHubError, match="refusing"):
        GhCliTransport(executable="/usr/bin/gh").get(path)


def test_gh_transport_reports_failures_without_a_response(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="error connecting to api.github.com")

    monkeypatch.setattr(github.subprocess, "run", fake_run)
    with pytest.raises(GitHubError, match="error connecting"):
        GhCliTransport(executable="/usr/bin/gh").get("repos/a/b")


def test_gh_transport_parses_error_responses(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        body = 'HTTP/2.0 404 Not Found\r\nContent-Type: application/json\r\n\r\n{"message":"Not Found"}'
        return subprocess.CompletedProcess(command, 1, stdout=body, stderr="gh: Not Found (HTTP 404)")

    monkeypatch.setattr(github.subprocess, "run", fake_run)
    api = GitHubApi(GhCliTransport(executable="/usr/bin/gh"))
    with pytest.raises(GitHubError) as info:
        api.get("repos/a/b")
    assert info.value.status == 404


def test_result_limit_beyond_max_pages_is_incomplete() -> None:
    full = [{"n": i} for i in range(100)]
    transport = FakeTransport({f"repos/a/b/pulls?page={i}&per_page=100": full for i in (1, 2)})
    with pytest.raises(GitHubError, match="incomplete"):
        GitHubApi(transport, max_pages=2).get_list("repos/a/b/pulls")
