# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The class rule for the FRICTION row of 2026-11-08T18:51Z that the 48 shipped rules left unclassified (OMN-22879).

The row is the text of a real ledger row (the cost cell and the prose the rule reads): a host install failed and ssh hung for
about 12 minutes because an unthrottled copy of a spill directory starved the host's init system. The control runs the same row
through the rules as they stood and the rollup reads it as ``unclassified``, so the comparison is able to fail.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from omnimarket.models.friction_rollup import (
    ModelFrictionClassRule,
    ModelFrictionRollupRequest,
    ModelFrictionRollupSource,
)
from omnimarket.nodes.node_friction_rollup_compute.handlers.handler_friction_rollup import (
    HandlerFrictionRollup,
)

pytestmark = pytest.mark.unit

OVERLAY = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_friction_rollup_compute/overlay.example.yaml"
)
NOW = datetime(2026, 11, 8, 19, 0, 0, tzinfo=UTC)
NEW_CLASS = "bulk-copy-on-host-array-starves-host-install"

ROW_TEXT = (
    "First host-ram-filesystem install on h051 failed with 'Reload daemon failed: Connection timed out' and ssh to h051 "
    "hung for about 12 minutes (load average 283) because an unthrottled rsync of the 133 GB ram-spill (millions of "
    "small files) on the rotational array starved systemd and devpi health checks; the copy of a spill directory must be "
    "bandwidth-limited from the start, and the installer should not run while a bulk copy reads the same array."
)
# Rows the new pattern must leave to their own classes: an rsync that is not an unthrottled copy of a spill, and a plain host hang.
OTHER_TEXTS = (
    "rsync of the worktree to the lab host took 4 minutes because the link was slow",
    "ssh to h051 hung for about 12 minutes while the host was swapping",
)


def shipped_rules() -> list[ModelFrictionClassRule]:
    data = yaml.safe_load(OVERLAY.read_text(encoding="utf-8"))
    return [ModelFrictionClassRule.model_validate(r) for r in data["class_rules"]]


def ledger_text(texts: tuple[str, ...]) -> str:
    lines = []
    for index, text in enumerate(texts):
        lines.append(
            f"2026-11-08T18:{51 + index:02d}:12Z | FRICTION | lane=lane-{index} | principal=p | ticket=OMN-22993 | "
            f"cost=20m | req=r{index} | via=bus:h200 | {text}"
        )
    return "\n".join(lines) + "\n"


def roll(
    rules: list[ModelFrictionClassRule], texts: tuple[str, ...]
) -> dict[str, dict[str, float]]:
    request = ModelFrictionRollupRequest(
        sources=[
            ModelFrictionRollupSource(
                display="ROLLING_WORK_LEDGER.md",
                kind="markdown",
                text=ledger_text(texts),
            )
        ],
        since=(NOW - timedelta(hours=12)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        until=NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
        generated_at=NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
        bucket_hours=2,
        umbrella_tickets=[],
        aliases=[],
        class_rules=rules,
    )
    report = HandlerFrictionRollup().handle(request).report
    return {
        item["class"]: {
            "occurrences": item["occurrences"],
            "cost_minutes": item["cost_minutes"],
        }
        for item in report["classes"]
    }


def test_shipped_rules_name_the_row() -> None:
    classes = roll(shipped_rules(), (ROW_TEXT,))
    assert classes == {NEW_CLASS: {"occurrences": 1, "cost_minutes": 20.0}}


def test_control_rules_as_they_stood_leave_the_row_unclassified() -> None:
    before = [rule for rule in shipped_rules() if rule.name != NEW_CLASS]
    assert len(before) == len(shipped_rules()) - 1
    assert roll(before, (ROW_TEXT,)) == {
        "unclassified": {"occurrences": 1, "cost_minutes": 20.0}
    }


def test_the_new_rule_names_only_its_own_row() -> None:
    classes = roll(shipped_rules(), (ROW_TEXT, *OTHER_TEXTS))
    assert classes[NEW_CLASS]["occurrences"] == 1
    assert classes["unclassified"]["occurrences"] == len(OTHER_TEXTS)


def test_the_new_rule_names_an_owner() -> None:
    rule = next(r for r in shipped_rules() if r.name == NEW_CLASS)
    assert len(rule.owner) > 40
