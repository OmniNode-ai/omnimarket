# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
# test-literal-ok: regex patterns used to assert contracts do NOT contain user paths
"""Structural tests for the work-tracking contracts move.

The 84 work-tracking ``OMN-XXXXX.yaml`` files moved from
``omnimarket/contracts/`` to ``omnimarket/docs/work-tracking/contracts/``.
The runtime layer never read them; they are pure dod_evidence artifacts.
These tests pin that invariant:

- ``contracts/OMN-*.yaml`` glob in repo root must be empty.
- Moved files must not contain user or volume absolute paths. Placeholder
  forms such as ``${OMNI_HOME}/...`` are allowed.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.unit
def test_no_omn_yamls_at_legacy_contracts_path() -> None:
    """`contracts/OMN-*.yaml` at the repo root must return zero matches."""
    legacy = REPO_ROOT / "contracts"
    if not legacy.exists():
        return  # directory removed, invariant trivially satisfied
    matches = list(legacy.glob("OMN-*.yaml"))
    assert matches == [], (
        f"legacy contracts/ still has OMN-*.yaml files: {[m.name for m in matches]}"
    )


@pytest.mark.unit
def test_work_tracking_contracts_dir_exists_and_populated() -> None:
    """Moved files must exist at the new canonical location."""
    new = REPO_ROOT / "docs" / "work-tracking" / "contracts"
    assert new.is_dir(), f"expected {new} to exist after the move"
    yamls = list(new.glob("OMN-*.yaml"))
    assert len(yamls) > 0, f"expected at least one OMN-*.yaml at {new}; found none"


@pytest.mark.unit
def test_work_tracking_contracts_have_no_user_paths() -> None:
    """The three flagged files were scrubbed; no work-tracking yaml should
    contain a ``/Users/<name>`` or ``/Volumes/<name>`` literal.

    Placeholder forms like ``${OMNI_HOME}/...`` are allowed.
    """
    new = REPO_ROOT / "docs" / "work-tracking" / "contracts"
    forbidden_patterns = (
        re.compile(r"/Users/[A-Za-z0-9._-]+"),
        re.compile(r"/Volumes/[A-Za-z0-9._-]+"),
    )
    findings: list[str] = []
    for yaml_path in sorted(new.glob("OMN-*.yaml")):
        text = yaml_path.read_text(encoding="utf-8")
        for pattern in forbidden_patterns:
            if match := pattern.search(text):
                findings.append(f"{yaml_path.name}: contains {match.group(0)!r}")
    assert findings == [], (
        f"work-tracking contracts contain user/volume paths: {findings}"
    )
