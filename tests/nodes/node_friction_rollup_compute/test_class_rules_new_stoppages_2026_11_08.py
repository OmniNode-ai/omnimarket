# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The class rules for the four FRICTION rows of 2026-11-08 that the 37 shipped rules left unclassified (OMN-23109).

Each row below is the text of a real ledger row (the part the rule reads). With the shipped rules every one names
its class; the control runs the same rows through the rules as they stood before this change and the rollup
reads them as ``unclassified``, so the comparison is able to fail.
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
NOW = datetime(2026, 11, 8, 6, 0, 0, tzinfo=UTC)

# (lane, text read by the rule, the class the shipped rules must name)
ROWS: tuple[tuple[str, str, str], ...] = (
    (
        "followups-2335",
        "Launching 13 successor lanes by hand cost about 25 minutes: lane_brief check slots (2) queue every brief for "
        'minutes; the remote-lane mint refusal regex reads the sentence "you cannot mint tickets" as a request to mint.',
        "lane-launch-by-hand-refused-by-brief-checks",
    ),
    (
        "tessl-fix-2315",
        "hook-preflight runs 3 to 5 minutes per commit (mypy 255 s, check-direct-model-call 139 s) and took about a "
        "third of the 90-minute box",
        "hook-preflight-minutes-per-commit",
    ),
    (
        "estimator-g-0315",
        "The canonical clone sync's dirty-converge node (node_canonical_clone_dirty_converge_effect) hard-resets the "
        "deploy agent's build clone to dev about 3 minutes into an off-lineage build",
        "clone-sync-dirty-converge-resets-deploy-build-clone",
    ),
    (
        "oneruntime-t13-0355",
        "Two commit gates conflict in omnibase_infra: the omnibase_core check-local-paths-compute hook reads whole "
        "files and flags five existing container-user lines, and its only escape (a local-path-ok marker) is refused "
        "by the canonical-file-shape ratchet as suppression growth",
        "commit-gates-conflict-local-paths-vs-file-shape-ratchet",
    ),
)
NEW_NAMES = {name for _, _, name in ROWS}


def shipped_rules() -> list[ModelFrictionClassRule]:
    document = yaml.safe_load(OVERLAY.read_text(encoding="utf-8"))
    return [ModelFrictionClassRule(**item) for item in document["class_rules"]]


def ledger_text() -> str:
    """FRICTION rows with no class= cell, one minute apart, inside the window."""
    lines = []
    for index, (lane, text, _) in enumerate(ROWS):
        stamp = (NOW - timedelta(hours=1, minutes=index)).strftime("%Y-%m-%dT%H:%M:%SZ")
        lines.append(
            f"{stamp} | FRICTION | lane={lane} | ticket=OMN-22865 | cost=~10 lane-minutes | {text}"
        )
    return "\n".join(lines) + "\n"


def roll(rules: list[ModelFrictionClassRule]) -> dict[str, int]:
    request = ModelFrictionRollupRequest(
        sources=[
            ModelFrictionRollupSource(
                display="ROLLING_WORK_LEDGER.md", kind="markdown", text=ledger_text()
            )
        ],
        since=(NOW - timedelta(hours=12)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        until=NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
        generated_at=NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
        bucket_hours=2,
        umbrella_tickets=["OMN-17427"],
        aliases=[],
        class_rules=rules,
    )
    report = HandlerFrictionRollup().handle(request).report
    return {item["class"]: item["occurrences"] for item in report["classes"]}


def test_shipped_rules_name_each_of_the_four_rows() -> None:
    classes = roll(shipped_rules())
    assert "unclassified" not in classes
    assert {name: classes.get(name) for name in NEW_NAMES} == dict.fromkeys(
        NEW_NAMES, 1
    )


def test_control_rules_without_the_new_four_leave_all_four_unclassified() -> None:
    before = [rule for rule in shipped_rules() if rule.name not in NEW_NAMES]
    assert len(before) == len(shipped_rules()) - len(NEW_NAMES)
    assert roll(before) == {"unclassified": len(ROWS)}


def test_each_new_rule_names_only_its_own_row() -> None:
    """A new rule that matched another row's text would steal it; each rule alone names exactly one row."""
    new_rules = [r for r in shipped_rules() if r.name in NEW_NAMES]
    assert len(new_rules) == len(ROWS)
    for rule in new_rules:
        without_others = [
            r for r in shipped_rules() if r.name not in NEW_NAMES or r.name == rule.name
        ]
        classes = roll(without_others)
        own = classes.pop(rule.name)
        assert own == 1, rule.name
        assert classes == {"unclassified": len(ROWS) - 1}, rule.name
