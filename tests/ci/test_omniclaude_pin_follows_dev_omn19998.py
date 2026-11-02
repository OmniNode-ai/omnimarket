# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19998: omniclaude consumes omnimarket by git pin, and no gate waits for a release.

Operator ruling 2026-10-16T07:34:18Z: "There's no reason we need to wait for a
release." omniclaude pins omnimarket to a dev commit in its uv.lock. Two
wirings here follow from that:

* a merge to dev dispatches ``omnimarket-dev-moved`` to omniclaude, whose
  sibling-lock-refresh.yml advances the pin (the event type is spelled in both
  repositories; a cross-repo test would skip wherever the other clone is
  absent, so each side pins its own spelling);
* the Wire Compatibility Gate grades the dev-merge tag omniclaude pins, read
  from omniclaude dev's pyproject.toml and uv.lock (OMN-21901 moved this from
  a commit sha parsed out of the lock), instead of the last release tag.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
DISPATCH_TYPE = "omnimarket-dev-moved"
GATE_SCRIPT = "scripts/ci/check_wire_compatibility.py"


def _load(name: str) -> dict[Any, Any]:
    loaded = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _gate_step_run() -> str:
    steps = _load("ci.yml")["jobs"]["wire-compatibility-gate"]["steps"]
    runs = [
        str(s.get("run", "")) for s in steps if GATE_SCRIPT in str(s.get("run", ""))
    ]
    assert len(runs) == 1, "the wire gate step is missing or duplicated"
    return runs[0]


def test_a_dev_merge_dispatches_the_pin_refresh_to_omniclaude() -> None:
    job = _load("runtime-rebuild-trigger.yml")["jobs"]["notify-omniclaude-pin"]
    condition = " ".join(str(job["if"]).split())
    assert "github.event.pull_request.merged == true" in condition
    assert "github.event.pull_request.base.ref == 'dev'" in condition
    # Public repository: hosted runners only (operator ruling 2026-10-16T07:07:09Z).
    assert job["runs-on"] == "ubuntu-latest"
    runs = "\n".join(str(s.get("run", "")) for s in job["steps"])
    assert "repos/OmniNode-ai/omniclaude/dispatches" in runs
    assert f"event_type={DISPATCH_TYPE}" in runs
    mint = next(s for s in job["steps"] if s.get("id") == "app-token")
    assert mint["with"]["repositories"] == "omniclaude"
    assert mint["with"]["permission-contents"] == "write"


def test_the_wire_gate_grades_omniclaudes_pinned_dev_tag() -> None:
    """OMN-21901: the step reads omniclaude dev's own pin files, never a sha."""
    run = _gate_step_run()
    assert "raw.githubusercontent.com/OmniNode-ai/omniclaude/dev" in run
    assert "/pyproject.toml" in run
    assert "/uv.lock" in run
    assert '--consumer-pyproject "omniclaude=' in run
    assert '--consumer-lock "omniclaude=' in run
    # No sha is parsed out of the lock, no raw ref is passed, no release is graded.
    assert "--consumer-ref" not in run
    assert "grep -oE" not in run
    assert "--grade-release-tag" not in run
    # A failed read of the pin files refuses; it never falls back to the last tag.
    assert "set -euo pipefail" in run
    assert "curl -fsSL" in run
    assert "|| true" not in run.split("curl")[1]


def test_the_wire_gate_step_names_every_declared_consumer() -> None:
    """The step supplies a pin for each consumer the script declares."""
    sys.path.insert(0, str(REPO_ROOT / "scripts" / "ci"))
    from check_wire_compatibility import PINNED_CONSUMERS

    run = _gate_step_run()
    for consumer in PINNED_CONSUMERS:
        assert f'--consumer-pyproject "{consumer}=' in run
        assert f'--consumer-lock "{consumer}=' in run
