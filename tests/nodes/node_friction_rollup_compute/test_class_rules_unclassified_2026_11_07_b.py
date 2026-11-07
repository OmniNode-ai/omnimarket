# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Class rules for the 9 FRICTION rows the earlier 26 rules left unclassified on 2026-11-07 (OMN-22865).

The fixture holds the 9 rows of the ledger window 2026-11-06T17:45Z to 2026-11-07T17:45Z that carried no class cell and
matched none of the rules shipped before this change: 146 lane-minutes. The shipped ``overlay.example.yaml`` is the rule set;
the control is the same request with only the earlier rules, which files every one of the rows under ``unclassified``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

import omnimarket.nodes.node_friction_rollup_compute as node_package
from omnimarket.models.friction_rollup import (
    ModelFrictionRollupRequest,
)
from omnimarket.nodes.node_friction_rollup_compute.handlers.handler_friction_rollup import (
    HandlerFrictionRollup,
)

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[3]
FIXTURE = ROOT / "tests/fixtures/friction_unclassified_rows_2026_11_07_b.md"
OVERLAY = Path(node_package.__file__).parent / "overlay.example.yaml"
RULES: list[dict[str, str]] = yaml.safe_load(OVERLAY.read_text(encoding="utf-8"))[
    "class_rules"
]
EARLIER_RULES = 26  # the rules shipped before this change; the seven rules of this change follow them
NEW_RULES = 7
CARRIED_LAST = 4  # rules of the floor friction change (OMN-22478) that follow the seven and name rows of 2026-11-05
FIXTURE_ROWS = 9
FIXTURE_MINUTES = 146.0

# class -> (rows, lane-minutes) the recorded text puts in it (measured from the fixture, not chosen)
EXPECTED = {
    "handoff-live-read-over-bus-failed": (1, 3.0),
    "plan-branch-absent-from-the-placed-lab-host-clone": (2, 25.0),
    "drive-bind-over-live-ramfs-ended-landing-workers": (1, 25.0),
    "ticket-falsifier-names-file-the-src-freeze-gate-refuses": (1, 30.0),
    "handoff-script-argument-list-too-long": (1, 18.0),
    "lane-github-read-budget-spent-on-job-logs": (2, 40.0),
    "ledger-write-ledger-option-ignored-on-bus-write-hosts": (1, 5.0),
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
            "since": "2026-11-06T17:45:00Z",
            "until": "2026-11-07T17:45:00Z",
            "generated_at": "2026-11-07T17:45:00Z",
            "bucket_hours": 2,
            "umbrella_tickets": [],
            "aliases": [],
            "class_rules": rules,
            "class_owners": [],
        }
    )
    return HandlerFrictionRollup().handle(request).report


def classes(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(item["class"]): item for item in report["classes"]}


def test_the_rule_list_still_opens_with_the_earlier_rules() -> None:
    assert len(RULES) == EARLIER_RULES + NEW_RULES + CARRIED_LAST
    assert RULES[EARLIER_RULES - 1]["name"] == "tool-result-exceeds-context-cap"


def test_with_only_the_earlier_rules_every_fixture_row_is_unclassified() -> None:
    """CONTROL: the rollup before this change. All 9 rows land in the one nameless class, with no owner."""
    report = roll(RULES[:EARLIER_RULES])
    unnamed = classes(report)["unclassified"]
    assert unnamed["rows"] == FIXTURE_ROWS
    assert unnamed["cost_minutes"] == FIXTURE_MINUTES
    assert report["totals"]["rows_without_class"] == FIXTURE_ROWS
    assert report["totals"]["rows_classified_by_rule"] == 0
    assert "owner" not in unnamed


def test_with_the_shipped_rules_no_fixture_row_is_unclassified() -> None:
    report = roll(RULES)
    assert "unclassified" not in classes(report)
    assert report["totals"]["rows_classified_by_rule"] == FIXTURE_ROWS


def test_each_recorded_cause_gets_its_own_class_and_an_owner() -> None:
    by_name = classes(roll(RULES))
    assert {
        name: (item["rows"], item["cost_minutes"]) for name, item in by_name.items()
    } == EXPECTED
    for name, item in by_name.items():
        assert item["owner"].strip(), name
        assert "operator" not in item["owner"].lower(), (name, item["owner"])


def test_every_new_rule_names_at_least_one_fixture_row() -> None:
    """A rule that matches nothing in the recorded rows is dead weight."""
    by_name = classes(roll(RULES))
    for rule in RULES[EARLIER_RULES : EARLIER_RULES + NEW_RULES]:
        assert rule["name"] in by_name, rule["name"]
