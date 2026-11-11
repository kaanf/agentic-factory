"""The committed evidence snapshots must match their generators and the fixture files they hash."""

from __future__ import annotations

import json

import pytest
from support import EVIDENCE, STATIC_SNAPSHOTS


@pytest.mark.parametrize("name", sorted(STATIC_SNAPSHOTS))
def test_committed_snapshot_is_current(name: str) -> None:
    committed = json.loads((EVIDENCE / name).read_text())
    assert committed == STATIC_SNAPSHOTS[name](), f"regenerate with: python tests/support.py ({name})"
