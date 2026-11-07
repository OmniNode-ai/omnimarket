# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Class rules for the 70 FRICTION rows the six earlier rules left unclassified (OMN-22865).

The fixture holds the rows of the ledger window 2026-11-06T09:07Z to 2026-11-07T10:40Z that carried no class cell and
matched none of the rules shipped before this change: 70 rows, 2,130 lane-minutes, which the floor board listed as the
red row ``stoppage:unclassified`` with no class and no owner. The shipped ``overlay.example.yaml`` is the rule set; the
control is the same request with only the six earlier rules, which is what the rollup does with every one of these rows.
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
FIXTURE = ROOT / "tests/fixtures/friction_unclassified_rows_2026_11_07.md"
OVERLAY = Path(node_package.__file__).parent / "overlay.example.yaml"
RULES: list[dict[str, str]] = yaml.safe_load(OVERLAY.read_text(encoding="utf-8"))[
    "class_rules"
]
EARLIER_RULES = 6  # the rules of OMN-22563, which stand first in the file and win ties
RULES_OF_THIS_FIXTURE = 20  # the rules that follow them and name this fixture's rows; later rules name later rows
FIXTURE_ROWS = 70

# class -> rows the recorded text puts in it (measured from the fixture, not chosen)
EXPECTED_ROWS = {
    "github-gateway-rejects-cache-only": 11,
    "handoff-install-first-read-fails": 5,
    "lab-host-table-refused-whole-by-row-model": 6,
    "lab-run-bus-unit-returns-no-receipt": 4,
    "lane-ledger-rows-reach-the-ledger-only-at-lane-end": 5,
    "ticket-create-gate-reads-sidecar-lane-name": 6,
    "lab-stack-unleased-ownership-unproved": 4,
    "launchd-bootstrap-io-error-after-install": 2,
    "read-tool-missing-for-the-lane-phase": 6,
    "runner-host-deploy-script-has-no-declared-route": 4,
    "scm-gateway-read-times-out": 1,
    "dev-redeploy-blocked-by-build-settings": 1,
    "canonical-clone-git-lock-left-behind": 2,
    "ci-timing-flake-in-validator-tests": 2,
    "receipted-delegation-route-fails": 2,
    "skill-or-tick-slice-shorter-than-measured-duration": 2,
    "concurrent-branch-installs-race-on-one-unit": 1,
    "landing-deferral-reason-not-read-at-handoff": 1,
    "lane-brief-premise-or-placement-does-not-hold-on-host": 4,
    "tool-result-exceeds-context-cap": 1,
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
            "since": "2026-11-06T09:00:00Z",
            "until": "2026-11-07T11:00:00Z",
            "generated_at": "2026-11-07T11:00:00Z",
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


def test_with_only_the_earlier_rules_every_fixture_row_is_unclassified() -> None:
    """CONTROL: the rollup before this change. All 70 rows land in the one nameless class, with no owner."""
    report = roll(RULES[:EARLIER_RULES])
    unnamed = classes(report)["unclassified"]
    assert unnamed["rows"] == FIXTURE_ROWS
    assert unnamed["cost_minutes"] == 2130.0
    assert report["totals"]["rows_without_class"] == FIXTURE_ROWS
    assert report["totals"]["rows_classified_by_rule"] == 0
    assert "owner" not in unnamed


def test_with_the_shipped_rules_no_fixture_row_is_unclassified() -> None:
    report = roll(RULES)
    by_name = classes(report)
    assert "unclassified" not in by_name
    assert report["totals"]["rows_classified_by_rule"] == FIXTURE_ROWS


def test_each_recorded_cause_gets_its_own_class_and_an_owner() -> None:
    by_name = classes(roll(RULES))
    assert {name: item["rows"] for name, item in by_name.items()} == EXPECTED_ROWS
    for name, item in by_name.items():
        assert item["owner"].strip(), name
        assert "operator" not in item["owner"].lower(), (name, item["owner"])


def test_the_host_table_class_is_the_rows_that_name_the_refused_column() -> None:
    """The largest class still occurring on 2026-11-07 (last row 09:09Z); its cause is the row model that forbids volumes_root."""
    by_name = classes(roll(RULES))
    item = by_name["lab-host-table-refused-whole-by-row-model"]
    assert item["last_seen"] == "2026-11-07T09:09:45Z"
    assert "ModelLabHostRow" in item["owner"]
    assert item["cost_minutes"] == 240.0


def test_every_rule_names_at_least_one_fixture_row_or_an_earlier_class() -> None:
    """A rule that matches nothing in the recorded rows is dead weight: only the earlier rules may sit unmatched here."""
    by_name = classes(roll(RULES))
    for rule in RULES[EARLIER_RULES : EARLIER_RULES + RULES_OF_THIS_FIXTURE]:
        assert rule["name"] in by_name, rule["name"]


def test_the_gateway_rule_stands_before_the_hand_off_rule() -> None:
    """Hand-off refusals that quote the cache_only error are the gateway's, not the hand-off check's."""
    names = [rule["name"] for rule in RULES]
    assert names.index("github-gateway-rejects-cache-only") < names.index(
        "handoff-install-first-read-fails"
    )
    swapped = [*RULES[:EARLIER_RULES]]
    by_name = {rule["name"]: rule for rule in RULES}
    swapped += [
        by_name["handoff-install-first-read-fails"],
        by_name["github-gateway-rejects-cache-only"],
        *[
            r
            for r in RULES[EARLIER_RULES:]
            if r["name"]
            not in (
                "handoff-install-first-read-fails",
                "github-gateway-rejects-cache-only",
            )
        ],
    ]
    # CONTROL: the other order moves rows from the gateway class to the hand-off class
    assert (
        classes(roll(swapped))["github-gateway-rejects-cache-only"]["rows"]
        < EXPECTED_ROWS["github-gateway-rejects-cache-only"]
    )
