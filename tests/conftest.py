from __future__ import annotations

import io
import json
import os
import socket
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from factory.cli import run  # noqa: E402

# Factory Core refuses group- or world-writable files and directories (fsread permission policy).
# Hosts with umask 002 (Ubuntu user private groups, GitHub runners) create exactly those, so Factory
# Core refuses checkouts and state created there; that is intended and tested explicitly
# (tests/unit/test_fsread_policy.py creates group-writable objects on purpose). The fixtures the tests
# build model a correctly configured host, so the test process uses umask 022. Factory Core itself
# never changes the umask.
os.umask(0o022)


class NetworkAccessError(RuntimeError):
    pass


def _blocked(*_args: Any, **_kwargs: Any) -> Any:
    raise NetworkAccessError("network access attempted during an offline test")


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test runs with outbound connections and name resolution disabled (T16)."""
    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    monkeypatch.setattr(socket, "getaddrinfo", _blocked)


CliResult = tuple[int, str, str]


@pytest.fixture
def cli() -> Callable[..., CliResult]:
    def invoke(*args: str | Path) -> CliResult:
        out, err = io.StringIO(), io.StringIO()
        code = run([str(a) for a in args], out=out, err=err)
        return code, out.getvalue(), err.getvalue()

    return invoke


@pytest.fixture
def plan_json(cli: Callable[..., CliResult], tmp_path: Path) -> Callable[[Path, dict[str, Any]], dict[str, Any]]:
    """Run ``factory plan --format json`` on a project with an in-memory snapshot."""
    from support import write_snapshot

    counter = iter(range(10_000))

    def invoke(project: Path, snapshot: dict[str, Any]) -> dict[str, Any]:
        path = write_snapshot(snapshot, tmp_path / f"snapshot-{next(counter)}.json")
        code, out, err = cli("plan", project, "--evidence", path, "--format", "json")
        assert code == 0, err or out
        result: dict[str, Any] = json.loads(out)
        return result

    return invoke


def task_of(plan: dict[str, Any], task_id: str) -> dict[str, Any]:
    return next(t for t in plan["tasks"] if t["id"] == task_id)


def codes(reasons: list[dict[str, Any]]) -> set[str]:
    """All reason codes in a reason tree."""
    found: set[str] = set()
    for reason in reasons:
        found.add(reason["code"])
        found |= codes(reason["causes"])
    return found
