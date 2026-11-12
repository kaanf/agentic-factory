"""Builders for Stage 5 tests: local checkouts, Archon home directories, run records and GitHub responses.

Checkouts are written as plain files in git's on-disk layout, so the reader is tested against exact
bytes; ``test_gitstate`` also checks it against repositories made by real ``git``. Every snapshot
here is SIMULATED: a READY task built from it authorizes nothing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from github_support import FakeTransport, page, q
from support import FIXTURES, approvals, fake_sha

from factory.github import GitHubApi, Response

DISPATCH = FIXTURES / "dispatch-project"
PROJECT_ID = "example-dispatch"
GITHUB = "example-org/example-app"
BASE = f"repos/{GITHUB}"
ORIGIN = f"https://github.com/{GITHUB}.git"
HEAD_SHA = fake_sha("app next head")


def make_checkout(
    root: Path,
    *,
    origin: str | None = ORIGIN,
    head: str = "next",
    branches: dict[str, str] | None = None,
    packed: dict[str, str] | None = None,
    remotes: dict[str, str] | None = None,
    worktrees: dict[str, str] | None = None,
    archon_base: str | None = "next",
    archon_text: str | None = None,
) -> Path:
    """A checkout at ``root``. ``branches`` and ``packed`` map branch names to SHAs (loose / packed refs),
    ``remotes`` maps origin branch names to SHAs, ``worktrees`` maps worktree ids to branches."""
    git = root / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "HEAD").write_text(f"ref: refs/heads/{head}\n")
    config = "[core]\n\trepositoryformatversion = 0\n\tbare = false\n"
    if origin is not None:
        config += f'[remote "origin"]\n\turl = {origin}\n\tfetch = +refs/heads/*:refs/remotes/origin/*\n'
    (git / "config").write_text(config)
    for name, sha in {"next": HEAD_SHA, **(branches or {})}.items():
        path = git / "refs" / "heads" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{sha}\n")
    if packed:
        lines = ["# pack-refs with: peeled fully-peeled sorted"]
        lines += [f"{sha} refs/heads/{name}" for name, sha in sorted(packed.items())]
        (git / "packed-refs").write_text("\n".join(lines) + "\n")
    for name, sha in (remotes or {}).items():
        path = git / "refs" / "remotes" / "origin" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{sha}\n")
    for worktree_id, branch in (worktrees or {}).items():
        entry = git / "worktrees" / worktree_id
        entry.mkdir(parents=True)
        (entry / "HEAD").write_text(f"ref: refs/heads/{branch}\n")
        (entry / "gitdir").write_text(f"/elsewhere/worktrees/{worktree_id}/.git\n")
    if archon_text is not None or archon_base is not None:
        (root / ".archon").mkdir()
        text = archon_text if archon_text is not None else f"worktree:\n  baseBranch: {archon_base}\n"
        (root / ".archon" / "config.yaml").write_text(text)
    return root


def make_archon_home(root: Path, *, version: str | None = "0.14.0", assistant: str | None = "claude") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    if version is not None:
        (root / "install.json").write_text(json.dumps({"binary": "/opt/archon/bin/archon", "version": version}))
    if assistant is not None:
        (root / "config.yaml").write_text(f"defaultAssistant: {assistant}\n")
    return root


def intent(task: str = "APP-1", revision: int = 3, attempt: int = 1, **changes: Any) -> dict[str, Any]:
    record = {
        "attempt": attempt,
        "base": "next",
        "branch": f"work/{task}-r{revision}",
        "createdAt": "2026-11-12T09:00:00Z",
        "host": "coordinator",
        "project": PROJECT_ID,
        "provider": "claude",
        "repository": "app",
        "revision": revision,
        "schemaVersion": "factory.run-intent/v1",
        "task": task,
        "workflow": "archon-implement",
    }
    record.update(changes)
    return record


def outcome(
    task: str = "APP-1",
    revision: int = 3,
    attempt: int = 1,
    status: str = "exited",
    exit_code: int | None = 0,
    **changes: Any,
) -> dict[str, Any]:
    record = {
        "attempt": attempt,
        "exitCode": exit_code,
        "finishedAt": "2026-11-12T09:30:00Z",
        "project": PROJECT_ID,
        "revision": revision,
        "schemaVersion": "factory.run-outcome/v1",
        "status": status,
        "task": task,
    }
    record.update(changes)
    return record


def record_dir(state: Path, task: str = "APP-1", revision: int = 3) -> Path:
    path = state / PROJECT_ID / "runs" / task / f"r{revision}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_record(state: Path, record: dict[str, Any], kind: str | None = None) -> Path:
    kind = kind or ("intent" if "createdAt" in record else "outcome")
    path = record_dir(state, record["task"], record["revision"]) / f"attempt-{record['attempt']}.{kind}.json"
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    return path


def ready_snapshot(root: Path = DISPATCH, pulls: dict[str, list[dict[str, Any]]] | None = None) -> dict[str, Any]:
    """Simulated evidence under which every task of the dispatch fixture is READY."""
    branches = {"work/APP-1-r3": {"pullRequests": []}, "work/APP-2-r1": {"pullRequests": []}}
    for branch, entries in (pulls or {}).items():
        branches[branch] = {"pullRequests": entries}
    return {
        "approvals": approvals(root, "example-human"),
        "project": PROJECT_ID,
        "provenance": {"kind": "simulated", "note": "test fixture; approvals are not authenticated"},
        "repositories": {"app": {"branches": branches, "commits": {}, "integrationHead": HEAD_SHA}},
        "schemaVersion": "factory.evidence-snapshot/v1",
    }


# -- GitHub responses ----------------------------------------------------------------------------


def gh_ref(branch: str) -> dict[str, Any]:
    return {"object": {"sha": fake_sha(branch), "type": "commit"}, "ref": f"refs/heads/{branch}"}


def gh_pull(
    number: int, branch: str, *, state: str = "open", merged: bool = False, base: str = "next"
) -> dict[str, Any]:
    return {
        "base": {"ref": base},
        "head": {"ref": branch, "repo": {"full_name": GITHUB}},
        "merge_commit_sha": fake_sha(f"merge {number}"),
        "merged_at": "2026-11-11T07:00:00Z" if merged else None,
        "number": number,
        "state": "closed" if merged else state,
    }


def remote_responses(
    task: str = "APP-1",
    revision: int = 3,
    *,
    remote_branches: list[str] | None = None,
    pulls: dict[int, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Responses for ``reconcile.read_remote``: matching refs plus one pull listing per revision."""
    responses: dict[str, Any] = {
        f"{BASE}/git/matching-refs/heads/work/{task}-r": [gh_ref(b) for b in remote_branches or []],
    }
    for r in range(1, revision + 1):
        responses[page(f"{BASE}/pulls", head=f"example-org:work/{task}-r{r}", state="all")] = (pulls or {}).get(r, [])
    return responses


def preflight_responses(
    task: str = "APP-1",
    revision: int = 3,
    *,
    remote_config: str | None = "worktree:\n  baseBranch: next\n",
    **kwargs: Any,
) -> dict[str, Any]:
    """Responses for a live preflight: remote facts plus the remote Archon config and exact branch ref."""
    import base64

    responses = remote_responses(task, revision, **kwargs)
    config_path = q(f"{BASE}/contents/.archon/config.yaml", ref="next")
    if remote_config is None:
        responses[config_path] = Response(404, {"message": "Not Found"})
    else:
        responses[config_path] = {
            "content": base64.b64encode(remote_config.encode()).decode(),
            "encoding": "base64",
            "type": "file",
        }
    branch = f"work/{task}-r{revision}"
    responses[f"{BASE}/git/matching-refs/heads/{branch}"] = [
        gh_ref(b) for b in kwargs.get("remote_branches") or [] if b.startswith(branch)
    ]
    return responses


def api(responses: dict[str, Any]) -> tuple[GitHubApi, FakeTransport]:
    transport = FakeTransport(responses)
    return GitHubApi(transport), transport
