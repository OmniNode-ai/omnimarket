# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The shipped class rules name every FRICTION row of the ledger day 2026-11-06T23:55Z to 2026-11-07T23:55Z that carried none (OMN-22865).

The rollup unit reads ``overlay.example.yaml`` of this node as its class rules. On that day it held six rules, so the floor
board's stoppage row ``stoppage:unclassified`` stood at 33 occurrences with no class and no owner, while the overlay home
already held 27 rules the unit never read. The fixture holds the 24 rows of that day that the first 27 rules left
unclassified (the lane, time and cost cells as recorded, the text cut to the words a rule reads). The control is the same
request with the six rules the unit read before this change, which leaves them unclassified.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

import omnimarket.nodes.node_friction_rollup_compute as node_package
from omnimarket.models.friction_rollup import ModelFrictionRollupRequest
from omnimarket.nodes.node_friction_rollup_compute.handlers.handler_friction_rollup import (
    HandlerFrictionRollup,
)

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[3]
FIXTURE = ROOT / "tests/fixtures/friction_class_rules_rows_2026_11_07.md"
OVERLAY = Path(node_package.__file__).parent / "overlay.example.yaml"
RULES: list[dict[str, str]] = yaml.safe_load(OVERLAY.read_text(encoding="utf-8"))[
    "class_rules"
]
# the rules the unit read before this change: the first six of the shipped set (the alarm, reaper, hook and install rules)
SIX_RULES_BEFORE = RULES[:6]
ROWS = 24

# lane -> the class the rules must give its row
EXPECTED = {
    "host-projection-c-1900": "brief-holds-window-misses-older-hold",
    "mkt-fix0-2000": "lane-github-read-budget-spent",
    "one-runtime-owner-1520": "lane-github-read-budget-spent",
    "host-bringup-b-1520": "lane-github-read-budget-spent",
    "twobackend-prs-2125": "lab-host-table-unusable-for-lab-run",
    "clobber-observer-2125": "lab-host-table-unusable-for-lab-run",
    "fill-ticket-OMN-22603-20261107T2247Z": "lab-host-table-unusable-for-lab-run",
    "liveness-gap-2220": "lab-host-table-unusable-for-lab-run",
    "estimator-deploy-2340": "lab-host-table-unusable-for-lab-run",
    "operator-commands-0130": "lab-host-table-unusable-for-lab-run",
    "runners-r2-0815": "lab-host-table-unusable-for-lab-run",
    "landing-owner-0230": "lab-host-table-unusable-for-lab-run",
    "one-runtime-d-2215": "pr-handoff-install-first-unprovable",
    "clobber-b-2215": "pr-handoff-install-first-unprovable",
    "estimator-ux-0130": "lab-host-lacks-node-toolchain",
    "floor-owner-0105": "brief-names-clone-or-path-absent-on-lab-host",
    "rb-j1-1215": "brief-names-clone-or-path-absent-on-lab-host",
    "floorfill-t14-1225": "brief-names-clone-or-path-absent-on-lab-host",
    "landing-owner-0900": "landing-deferred-by-criteria-drift",
    "ground-state-report": "bus-state-read-unavailable-to-lane",
    "dropped-work": "bus-state-read-unavailable-to-lane",
    "session-goal": "bus-state-read-unavailable-to-lane",
    "h053-nvme-1255": "landing-workers-ended-by-host-drive-bind",
    "prsred-c-1420": "pr-handoff-argument-list-too-long",
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
            "since": "2026-11-06T23:55:00Z",
            "until": "2026-11-08T00:00:00Z",
            "generated_at": "2026-11-08T00:00:00Z",
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


def test_control_the_six_rules_the_unit_read_before_leave_every_row_unclassified() -> (
    None
):
    """CONTROL: with the rules the unit read on 2026-11-07, the floor's nameless class holds the whole fixture."""
    report = roll(SIX_RULES_BEFORE)
    assert report["totals"]["rows_without_class"] == ROWS
    unnamed = classes(report)["unclassified"]
    assert unnamed["rows"] == ROWS
    assert "owner" not in unnamed


def test_the_shipped_rules_leave_no_row_unclassified() -> None:
    report = roll(RULES)
    by_name = classes(report)
    assert "unclassified" not in by_name
    assert report["totals"]["rows_classified_by_rule"] == ROWS


def test_every_row_gets_its_class_and_every_class_an_owner() -> None:
    by_name = classes(roll(RULES))
    got = {lane: name for name, item in by_name.items() for lane in item["lanes"]}
    assert got == EXPECTED
    for name, item in by_name.items():
        assert item["owner"].strip(), name
        assert "operator" not in item["owner"].lower(), (name, item["owner"])


def test_the_largest_class_is_the_lab_host_table_with_eight_rows() -> None:
    by_name = classes(roll(RULES))
    largest = max(by_name, key=lambda name: by_name[name]["rows"])
    assert (largest, by_name[largest]["rows"]) == (
        "lab-host-table-unusable-for-lab-run",
        8,
    )
    assert sum(item["rows"] for item in by_name.values()) == ROWS


def test_control_a_row_no_rule_names_stays_unclassified() -> None:
    """CONTROL: the new rules are not a catch-all; a row with no known cause is still counted as unclassified."""
    text = (
        "2026-11-07T12:00:00Z | FRICTION | lane=plain-lane-0000 | ticket=OMN-22865 | cost=~5 lane-minutes | "
        "the sandbox clock drifted by two seconds and one test was retried once\n"
    )
    request = ModelFrictionRollupRequest.model_validate(
        {
            "sources": [{"display": "ledger", "kind": "markdown", "text": text}],
            "since": "2026-11-07T00:00:00Z",
            "until": "2026-11-08T00:00:00Z",
            "generated_at": "2026-11-08T00:00:00Z",
            "bucket_hours": 2,
            "umbrella_tickets": [],
            "aliases": [],
            "class_rules": RULES,
            "class_owners": [],
        }
    )
    report = HandlerFrictionRollup().handle(request).report
    assert classes(report)["unclassified"]["rows"] == 1
