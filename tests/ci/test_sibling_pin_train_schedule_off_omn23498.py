# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18594: sibling-pin-train.yml runs by workflow_dispatch only, with a dry_run.

RULING 2026-11-09T21:54:25Z lane=orchestrator-31d9 (OMN-18594): downstream pins
move only when the downstream needs something new; the nightly schedule is
gone and the train runs by hand dispatch that can print its plan without
publishing.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "sibling-pin-train.yml"


def _load(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text())


def train_workflow_violations(doc: dict) -> list[str]:
    violations: list[str] = []
    triggers = doc.get(True) or doc.get("on") or {}
    if "schedule" in triggers:
        violations.append("still runs on a schedule")
    if set(triggers) != {"workflow_dispatch"}:
        violations.append("has a trigger other than workflow_dispatch")
    if not (triggers.get("workflow_dispatch") or {}).get("inputs", {}).get("dry_run"):
        violations.append("has no workflow_dispatch dry_run input")
    return violations


def test_train_workflow_holds_on_the_live_workflow() -> None:
    assert train_workflow_violations(_load(WORKFLOW)) == []


def test_train_workflow_flags_a_restored_schedule() -> None:
    doc = copy.deepcopy(_load(WORKFLOW))
    doc[True]["schedule"] = [{"cron": "41 6 * * *"}]
    assert "still runs on a schedule" in train_workflow_violations(doc)


def test_train_workflow_flags_a_missing_dry_run_input() -> None:
    doc = copy.deepcopy(_load(WORKFLOW))
    del doc[True]["workflow_dispatch"]["inputs"]["dry_run"]
    assert "has no workflow_dispatch dry_run input" in train_workflow_violations(doc)


def test_train_workflow_cites_the_ruling_above_the_trigger_block() -> None:
    text = WORKFLOW.read_text()
    head, _, _ = text.partition("\non:\n")
    assert "RULING 2026-11-09T21:54:25Z lane=orchestrator-31d9 (OMN-18594)" in head
