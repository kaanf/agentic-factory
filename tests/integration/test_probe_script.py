"""The opt-in Archon probe script refuses to do anything without its explicit authorization.

Only the refusal paths run here, against a stand-in ``archon`` that records any call. The probe
itself is never run by the test suite.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from stage5_support import make_checkout

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "probes" / "archon-base-branch-probe.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not installed")


def _run(tmp_path: Path, checkout: Path, *args: str, authorized: str | None) -> tuple[int, str, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    marker = tmp_path / "archon-was-called"
    fake = bin_dir / "archon"
    fake.write_text(f"#!/bin/sh\necho called >> '{marker}'\necho 'archon 0.14.0'\n")
    fake.chmod(0o755)
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path)}
    if authorized is not None:
        env["FACTORY_ARCHON_PROBE_AUTHORIZED"] = authorized
    result = subprocess.run(
        ["bash", str(SCRIPT), *args], cwd=tmp_path, env=env, capture_output=True, text=True, check=False
    )
    return result.returncode, result.stderr, marker


def _args(checkout: Path, *extra: str) -> list[str]:
    return [
        "--i-have-read-docs-probes-archon-probes-md",
        "--checkout", str(checkout),
        "--integration", "next",
        "--release", "stable",
        "--expected-version", "0.14.0",
        *extra,
    ]  # fmt: skip


def _snapshot(root: Path) -> dict[str, tuple[int, int]]:
    return {str(p): (p.stat().st_mtime_ns, p.stat().st_size) for p in sorted(root.rglob("*"))}


def test_without_authorization_nothing_runs(tmp_path: Path) -> None:
    checkout = make_checkout(tmp_path / "checkout")
    before = _snapshot(checkout)
    code, err, marker = _run(tmp_path, checkout, *_args(checkout), authorized=None)
    assert code == 2 and "refused" in err
    assert not marker.exists()
    assert _snapshot(checkout) == before


def test_authorization_for_another_checkout_is_refused(tmp_path: Path) -> None:
    checkout = make_checkout(tmp_path / "checkout")
    code, _, marker = _run(tmp_path, checkout, *_args(checkout), authorized=str(tmp_path / "elsewhere"))
    assert code == 2 and not marker.exists()


def test_missing_acknowledgement_is_a_usage_error(tmp_path: Path) -> None:
    checkout = make_checkout(tmp_path / "checkout")
    args = _args(checkout)[1:]
    code, _, marker = _run(tmp_path, checkout, *args, authorized=str(checkout.resolve()))
    assert code == 64 and not marker.exists()


@pytest.mark.parametrize("branch", ["-x", "a b", "$(id)", "next;rm"])
def test_unsafe_branch_names_are_refused_before_archon_runs(tmp_path: Path, branch: str) -> None:
    checkout = make_checkout(tmp_path / "checkout")
    args = _args(checkout)
    args[args.index("--integration") + 1] = branch
    code, _, marker = _run(tmp_path, checkout, *args, authorized=str(checkout.resolve()))
    assert code == 2 and not marker.exists()


def test_script_is_not_referenced_by_factory_core() -> None:
    source = Path(__file__).resolve().parents[2] / "src"
    assert not any("archon-base-branch-probe" in p.read_text() for p in source.rglob("*.py"))
    assert os.access(SCRIPT, os.X_OK)
