# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Class rules for the two rows the 28 rules of OMN-22865 and the earlier OMN-22879 change left unclassified (OMN-22879).

The fixture holds the two FRICTION rows of the ledger window 2026-11-06T15:44Z to 2026-11-07T15:44Z that carried no class cell
and matched none of the 28 rules: 55 lane-minutes. The control is the same request with only those 28 rules, which is what the
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
FIXTURE = ROOT / "tests/fixtures/friction_unclassified_rows_2026_11_07_d.md"
OVERLAY = Path(node_package.__file__).parent / "overlay.example.yaml"
RULES: list[dict[str, str]] = yaml.safe_load(OVERLAY.read_text(encoding="utf-8"))[
    "class_rules"
]
RULES_BEFORE = 28  # the six rules of OMN-22563, the twenty of OMN-22865 and the two of the first OMN-22879 change
FIXTURE_ROWS = 2
EXPECTED_ROWS = {
    "host-install-binds-drive-over-live-ram-filesystem": 1,
    "criterion-falsifier-names-a-new-code-file-the-src-freeze-gate-refuses": 1,
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
            "since": "2026-11-06T15:00:00Z",
            "until": "2026-11-07T16:00:00Z",
            "generated_at": "2026-11-07T16:00:00Z",
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


def test_the_rules_this_change_adds_are_the_two_after_the_earlier_twenty_eight() -> (
    None
):
    assert [
        rule["name"] for rule in RULES[RULES_BEFORE : RULES_BEFORE + FIXTURE_ROWS]
    ] == list(EXPECTED_ROWS)


def test_with_only_the_earlier_rules_both_rows_are_unclassified() -> None:
    """CONTROL: the rollup before this change files both rows under the one nameless class, with no owner."""
    report = roll(RULES[:RULES_BEFORE])
    unnamed = classes(report)["unclassified"]
    assert unnamed["rows"] == FIXTURE_ROWS
    assert unnamed["cost_minutes"] == 55.0
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
