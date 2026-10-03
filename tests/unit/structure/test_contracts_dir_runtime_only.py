# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
# test-literal-ok: regex patterns used to assert contracts do NOT contain user paths
"""Structural tests for the work-tracking contracts move.

The 84 work-tracking ``OMN-XXXXX.yaml`` files moved from
``omnimarket/contracts/`` to ``omnimarket/docs/work-tracking/contracts/``.
The runtime layer never read them; they are pure dod_evidence artifacts.
These tests pin that invariant:

- No file moved to ``docs/work-tracking/contracts/`` may reappear under
  ``contracts/``. ``contracts/OMN-<n>.yaml`` is the repo-owned DoD evidence
  contract that the Repo Evidence Gate (caller-evidence mode) reads at the
  pull request head, so the directory is no longer required to be empty.
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
    """A work-tracking contract that moved must not return to `contracts/`."""
    legacy = REPO_ROOT / "contracts"
    if not legacy.exists():
        return  # directory removed, invariant trivially satisfied
    moved = {
        p.name
        for p in (REPO_ROOT / "docs" / "work-tracking" / "contracts").glob("OMN-*.yaml")
    }
    returned = sorted(m.name for m in legacy.glob("OMN-*.yaml") if m.name in moved)
    assert returned == [], (
        f"work-tracking contracts moved out of contracts/ but are back: {returned}"
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
