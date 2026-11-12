"""Probe policy (R3): no executable Archon probe ships with Factory Core.

The former opt-in probe script recognized hook configuration with text matching, which valid YAML
(flow mappings, quoted keys, JSON, anchors and merge keys) bypassed, so `postCreate` could run when
Archon created a worktree. A reliable execution boundary could not be established (Archon's effective
configuration includes database-held layers, and an inspected file can change before Archon reads
it), so execution was removed. These tests keep it removed and keep the procedure honest.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROCEDURE = ROOT / "docs" / "probes" / "archon-probes.md"
_ARCHON_RUN = re.compile(r"\barchon\s+(workflow\s+run|serve|trigger)\b")


def _shipped_files() -> list[Path]:
    skip = {".git", ".venv", "docs", "tests", "__pycache__", ".ruff_cache", "dist"}
    found = []
    for path in ROOT.rglob("*"):
        if path.is_file() and not skip.intersection(path.relative_to(ROOT).parts):
            found.append(path)
    return found


def test_no_executable_probe_or_archon_invocation_ships() -> None:
    assert not (ROOT / "scripts" / "probes").exists()
    offenders = []
    for path in _shipped_files():
        if path.suffix in {".sh", ".bash", ".zsh"} or (os.access(path, os.X_OK) and path.suffix not in {".py"}):
            offenders.append(str(path.relative_to(ROOT)))
        elif path.suffix in {".py", ".toml", ".yml", ".yaml", ".json"}:
            if _ARCHON_RUN.search(path.read_text(errors="replace")):
                offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []


def test_procedure_states_that_execution_is_disabled_and_acceptance_incomplete() -> None:
    text = PROCEDURE.read_text()
    assert "Execution is disabled" in text
    assert "acceptance item remains incomplete" in text
    assert "workflow run archon-implement" not in text
    assert "--dry-run" not in text  # no inert-by-one-flag invocations of agent workflows
    for claim in ("harmless", "risk-free", "safe because it is bash"):
        assert claim not in text.lower()
