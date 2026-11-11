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


def _git_checkout(tmp_path: Path, files: dict[str, str]) -> Path:
    """A real, clean git repository holding ``files`` (committed)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=repo, env=env, capture_output=True, check=True)

    git("init", "--quiet", "--initial-branch=next")
    for name, content in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(content)
    git("add", "-A")
    git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "--quiet", "--allow-empty", "-m", "c")
    return repo


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
@pytest.mark.parametrize(
    "files",
    [
        {".archon/config.yaml": "worktree:\n  baseBranch: next\n  postCreate: ./setup.sh\n"},
        {".archon/config.yaml": "worktree:\n  baseBranch: next\n  copyFiles: [.env]\n"},
        {".archon/config.yaml": "worktree:\n  baseBranch: next\nassistant: codex\n"},
        {".archon/config.yaml": "worktree:\n  baseBranch: next\ntiers:\n  large: {provider: codex, model: x}\n"},
        {".archon/config.yaml": "worktree:\n  baseBranch: next\nenv:\n  DEFAULT_AI_ASSISTANT: codex\n"},
        {".archon/config.yaml": "worktree:\n  baseBranch: next\n", ".archon/workflows/other.yaml": "name: other\n"},
    ],
    ids=["postCreate", "copyFiles", "assistant", "tiers", "env", "existing-workflows"],
)
def test_hooks_and_overrides_are_refused_before_archon_runs(tmp_path: Path, files: dict[str, str]) -> None:
    checkout = _git_checkout(tmp_path, files)
    before = _snapshot(checkout)
    code, err, marker = _run(tmp_path, checkout, *_args(checkout), authorized=str(checkout.resolve()))
    assert code == 2 and "refused" in err, err
    assert not marker.exists()  # Archon was never started, not even for --version
    assert _snapshot(checkout) == before


def test_probe_procedure_never_invokes_a_coding_workflow() -> None:
    script = SCRIPT.read_text()
    procedure = (SCRIPT.parents[2] / "docs" / "probes" / "archon-probes.md").read_text()
    for text in (script, procedure):
        assert "workflow run archon-implement" not in text
    assert "prompt" not in script.split('cat > "$workflow_file" <<YAML', 1)[1].split("YAML", 1)[0]
