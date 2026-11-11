"""Fake and recording GitHub transports, plus builders for GitHub-shaped API responses.

Builders produce the fields the collector reads, in the shapes the REST API returns them.
``python tests/github_support.py`` re-records the real responses for the self-describing fixture
project (read-only GET requests against the public kaanf/agentic-factory repository).
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path
from typing import Any

from factory.github import GhCliTransport, Response

FIXTURES = Path(__file__).parent / "fixtures"
RECORDED = FIXTURES / "github" / "agentic-factory-recorded.json"
SELF_PROJECT = FIXTURES / "agentic-factory-project"

REPO = "example-org/example-svc"
BASE = f"repos/{REPO}"


class Sequence(list):  # type: ignore[type-arg]
    """Successive responses for one path; the last one repeats."""


class FakeTransport:
    """Serves canned responses by exact request path. Any other request fails the test."""

    def __init__(self, responses: dict[str, Response | Any]) -> None:
        self.responses = dict(responses)
        self.requests: list[str] = []

    def get(self, path: str) -> Response:
        self.requests.append(path)
        if path not in self.responses:
            raise AssertionError(f"unexpected GitHub request: {path}")
        value = self.responses[path]
        if isinstance(value, Sequence):
            value = value.pop(0) if len(value) > 1 else value[0]
        if isinstance(value, Exception):
            raise value
        return value if isinstance(value, Response) else Response(200, value)


class RecordingTransport:
    def __init__(self, inner: GhCliTransport) -> None:
        self.inner = inner
        self.recorded: dict[str, dict[str, Any]] = {}

    def get(self, path: str) -> Response:
        response = self.inner.get(path)
        self.recorded[path] = {"body": response.body, "status": response.status}
        return response


def load_recorded() -> FakeTransport:
    data = json.loads(RECORDED.read_text())
    return FakeTransport({path: Response(entry["status"], entry["body"]) for path, entry in data.items()})


# -- request paths --------------------------------------------------------------


def q(path: str, **params: str | int) -> str:
    from urllib.parse import urlencode

    return f"{path}?{urlencode(sorted(params.items()))}" if params else path


def page(path: str, **params: str | int) -> str:
    return q(path, page=1, per_page=100, **params)


# -- response builders -----------------------------------------------------------


def identity(full_name: str = REPO) -> dict[str, Any]:
    return {"full_name": full_name, "private": False}


def ref(sha: str) -> dict[str, Any]:
    return {"object": {"sha": sha, "type": "commit"}, "ref": "refs/heads/x"}


def pull(
    number: int,
    branch: str,
    *,
    base: str = "dev",
    state: str = "closed",
    merged_sha: str | None = None,
    head_repo: str = REPO,
    test_merge_sha: str = "f" * 40,
) -> dict[str, Any]:
    return {
        "base": {"ref": base},
        "body": "All checks passed, ready to merge!",  # never read by the collector
        "head": {"ref": branch, "repo": {"full_name": head_repo}},
        "merge_commit_sha": merged_sha or test_merge_sha,
        "merged_at": "2026-11-11T07:00:00Z" if merged_sha else None,
        "number": number,
        "state": state,
    }


def check_run(
    check_id: int,
    sha: str,
    *,
    name: str = "Svc Check",
    suite: int = 1,
    app: str = "github-actions",
    status: str = "completed",
    conclusion: str | None = "success",
) -> dict[str, Any]:
    return {
        "app": {"id": 15368 if app == "github-actions" else 999, "slug": app},
        "check_suite": {"id": suite},
        "conclusion": conclusion,
        "head_sha": sha,
        "id": check_id,
        "name": name,
        "status": status,
    }


def workflow_run(
    run_id: int,
    sha: str,
    *,
    suite: int = 1,
    event: str = "push",
    branch: str = "dev",
    path: str = ".github/workflows/ci.yml",
    attempt: int = 1,
) -> dict[str, Any]:
    return {
        "check_suite_id": suite,
        "event": event,
        "head_branch": branch,
        "head_sha": sha,
        "id": run_id,
        "path": path,
        "run_attempt": attempt,
    }


def job(check_id: int, attempt: int = 1) -> dict[str, Any]:
    return {
        "check_run_url": f"https://api.github.com/{BASE}/check-runs/{check_id}",
        "id": check_id,
        "run_attempt": attempt,
    }


def listing(key: str, items: list[Any]) -> dict[str, Any]:
    return {key: items, "total_count": len(items)}


def contents(text: str) -> dict[str, Any]:
    return {"content": base64.b64encode(text.encode()).decode(), "encoding": "base64", "type": "file"}


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
    from factory.collector import collect
    from factory.github import GitHubApi
    from factory.project import load_project

    project = load_project(SELF_PROJECT).project
    assert project is not None
    recorder = RecordingTransport(GhCliTransport())
    result = collect(project, GitHubApi(recorder))
    RECORDED.parent.mkdir(parents=True, exist_ok=True)
    RECORDED.write_text(json.dumps(recorder.recorded, indent=2, sort_keys=True) + "\n")
    print(f"recorded {len(recorder.recorded)} responses to {RECORDED}; collection errors: {len(result.errors)}")
