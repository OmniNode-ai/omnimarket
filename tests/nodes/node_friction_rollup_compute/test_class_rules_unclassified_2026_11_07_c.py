# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Class rules for the two rows the 26 rules of OMN-22865 left unclassified (OMN-22879).

The fixture holds the two FRICTION rows of the ledger window 2026-11-06T13:26Z to 2026-11-07T13:26Z that carried no class cell
and matched none of the 26 rules: 13 lane-minutes. The control is the same request with only those 26 rules, which is what the
rollup does with both rows.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

import omnimarket.nodes.node_friction_rollup_compute as node_package
from omnimarket.nodes.node_friction_rollup_compute.handlers.handler_friction_rollup import (
    HandlerFrictionRollup,
)
from omnimarket.nodes.node_friction_rollup_compute.models.model_friction_rollup import (
    ModelFrictionRollupRequest,
)

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[3]
FIXTURE = ROOT / "tests/fixtures/friction_unclassified_rows_2026_11_07_c.md"
OVERLAY = Path(node_package.__file__).parent / "overlay.example.yaml"
RULES: list[dict[str, str]] = yaml.safe_load(OVERLAY.read_text(encoding="utf-8"))[
    "class_rules"
]
RULES_BEFORE = 26  # the six rules of OMN-22563 and the twenty of OMN-22865
FIXTURE_ROWS = 2
EXPECTED_ROWS = {
    "scm-pr-view-bus-live-read-fails": 1,
    "canonical-clone-sync-answers-no-clone-for-a-brief-repository": 1,
}


def roll(rules: list[dict[str, str]]) -> dict[str, Any]:
    request = ModelFrictionRollupRequest.model_validate(
        {
            "sources": [
                {
                    "display": "ledger",
                    "kind": "markdown",
                    "text": FIXTURE.read_text(encoding="utf-8"),
                }
            ],
            "since": "2026-11-06T13:00:00Z",
            "until": "2026-11-07T14:00:00Z",
            "generated_at": "2026-11-07T14:00:00Z",
            "bucket_hours": 2,
            "umbrella_tickets": ["OMN-17427"],
            "aliases": [],
            "class_rules": rules,
            "class_owners": [],
        }
    )
    return HandlerFrictionRollup().handle(request).report


def classes(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(item["class"]): item for item in report["classes"]}


def test_the_rules_this_change_adds_are_the_two_after_the_earlier_twenty_six() -> None:
    assert [
        rule["name"] for rule in RULES[RULES_BEFORE : RULES_BEFORE + FIXTURE_ROWS]
    ] == list(EXPECTED_ROWS)


def test_with_only_the_earlier_rules_both_rows_are_unclassified() -> None:
    """CONTROL: the rollup before this change files both rows under the one nameless class, with no owner."""
    report = roll(RULES[:RULES_BEFORE])
    unnamed = classes(report)["unclassified"]
    assert unnamed["rows"] == FIXTURE_ROWS
    assert unnamed["cost_minutes"] == 13.0
    assert report["totals"]["rows_classified_by_rule"] == 0
    assert "owner" not in unnamed


def test_with_the_shipped_rules_no_row_is_unclassified_and_each_class_has_an_owner() -> (
    None
):
    report = roll(RULES)
    by_name = classes(report)
    assert "unclassified" not in by_name
    assert {name: item["rows"] for name, item in by_name.items()} == EXPECTED_ROWS
    assert report["totals"]["rows_classified_by_rule"] == FIXTURE_ROWS
    for name, item in by_name.items():
        assert item["owner"].strip(), name
        assert "operator" not in item["owner"].lower(), (name, item["owner"])
