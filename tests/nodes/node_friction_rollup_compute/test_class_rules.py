# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Class rules for FRICTION rows written with no class cell, replayed from the recorded rows (OMN-22563).

The fixture holds rows of the ledger window 2026-11-05T01:07Z to 2026-11-06T01:07Z, in which the rollup filed 63 of 352
FRICTION rows (1,216 lane-minutes) under ``unclassified``, so the floor board listed a stoppage with no class and no
owner. The shipped ``overlay.example.yaml`` is the rule set; the control is the same request with no rules, which is what
origin/dev does with every one of these rows.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

import omnimarket.nodes.node_friction_rollup_compute as node_package
from omnimarket.nodes.node_friction_rollup_compute.handlers.handler_friction_rollup import (
    HandlerFrictionRollup,
)
from omnimarket.nodes.node_friction_rollup_compute.models.model_friction_rollup import (
    ModelFrictionRollupRequest,
)

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[3]
FIXTURE = ROOT / "tests/fixtures/friction_class_rules_rows.md"
OVERLAY = Path(node_package.__file__).parent / "overlay.example.yaml"
_OVERLAY = yaml.safe_load(OVERLAY.read_text(encoding="utf-8"))
RULES: list[dict[str, str]] = _OVERLAY["class_rules"]
OWNERS: list[dict[str, str]] = _OVERLAY["class_owners"]

# lane -> the class the rules must give its recorded row
EXPECTED = {
    "lab-stack-reaper": {
        "lab-stack-reaper-no-docker-daemon",
        "lab-stack-reaper-pass-refused",
        "ledger-window-file-unreadable",
    },
    "b0-m0-1530": {"local-hooks-fail-one-per-attempt"},
    "dropped-prs-1545": {"local-hooks-fail-one-per-attempt"},
    "review-keystone-1845": {"local-hooks-fail-one-per-attempt"},
    "m0-reader-2410": {"installed-copy-behind-merged-code"},
}
ALARM_SOURCES = {
    "lab-pass-receipt",
    "consumer-group-lag",
    "dev-tag-heartbeat",
    "stale-indeterminate",
}  # the rollup normalises _ to -
# recorded hand-written rows that name no recognisable family: they must stay unclassified
UNMATCHED_LANES = {"temporal-e1-0630", "dropped-work", "h053-bringup-1900"}


def roll(
    rules: list[dict[str, str]], owners: list[dict[str, str]] | None = None
) -> dict[str, Any]:
    request = ModelFrictionRollupRequest.model_validate(
        {
            "sources": [
                {
                    "display": "ledger",
                    "kind": "markdown",
                    "text": FIXTURE.read_text(encoding="utf-8"),
                }
            ],
            "since": "2026-11-05T01:00:00Z",
            "until": "2026-11-06T04:00:00Z",
            "generated_at": "2026-11-06T04:00:00Z",
            "bucket_hours": 2,
            "umbrella_tickets": ["OMN-17427"],
            "aliases": [],
            "class_rules": rules,
            "class_owners": owners or [],
        }
    )
    return HandlerFrictionRollup().handle(request).report


def classes(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(item["class"]): item for item in report["classes"]}


def lanes_of(report: dict[str, Any], name: str) -> set[str]:
    return set(classes(report)[name]["lanes"])


def test_without_rules_every_classless_row_is_unclassified() -> None:
    """CONTROL: origin/dev's behaviour. The reaper, the alarm and the hook rows all land in the one nameless class."""
    report = roll([])
    unnamed = classes(report)["unclassified"]
    assert {"lab-stack-reaper", "lab-alarm", "b0-m0-1530", "m0-reader-2410"} <= set(
        unnamed["lanes"]
    )
    assert report["totals"]["rows_without_class"] == unnamed["rows"]
    assert "owner" not in unnamed


def test_the_recorded_rows_get_their_named_classes_and_an_owner() -> None:
    report = roll(RULES)
    by_name = classes(report)
    for lane, names in EXPECTED.items():
        got = {
            name
            for name, item in by_name.items()
            if lane in item["lanes"] and name != "unclassified"
        }
        assert got, lane
        assert got <= names, (lane, got)
    assert {f"lab-alarm:{source}" for source in ALARM_SOURCES} <= set(by_name)
    for name, item in by_name.items():
        if item.get("rows_ruled"):
            assert item["owner"].strip(), name
            assert "operator" not in item["owner"].lower(), (name, item["owner"])
    assert report["totals"]["rows_classified_by_rule"] == sum(
        i.get("rows_ruled", 0) for i in by_name.values()
    )


def test_the_reaper_rows_that_read_the_same_text_split_into_the_three_causes() -> None:
    by_name = classes(roll(RULES))
    assert by_name["lab-stack-reaper-no-docker-daemon"]["rows"] == 3
    assert (
        by_name["lab-stack-reaper-pass-refused"]["rows"] == 2
    )  # no such object; no space left on device
    assert by_name["ledger-window-file-unreadable"]["rows"] == 2


def test_a_row_with_a_class_cell_keeps_its_own_class_whatever_the_rules_say() -> None:
    greedy = [
        *RULES,
        {"name": "everything", "owner": "nobody", "pattern": "FRICTION|lab|hook|refus"},
    ]
    by_name = classes(roll(greedy))
    assert by_name["lab-idle"]["lanes"] == ["lab-fill"]
    assert "refusal:unauthorised-shared-tree-git-mutation" in by_name


def test_a_row_no_rule_matches_stays_unclassified() -> None:
    by_name = classes(roll(RULES))
    assert lanes_of(roll(RULES), "unclassified") >= UNMATCHED_LANES
    assert "owner" not in by_name["unclassified"]
    assert not (
        set(by_name["unclassified"]["lanes"]) & {"lab-stack-reaper", "lab-alarm"}
    )


def test_the_first_matching_rule_names_the_class() -> None:
    first = [{"name": "first", "owner": "a", "pattern": "docker API"}, *RULES]
    by_name = classes(roll(first))
    assert by_name["first"]["rows"] == 3
    assert "lab-stack-reaper-no-docker-daemon" not in by_name


@pytest.mark.parametrize(
    "rule",
    [
        {"name": "x", "owner": "", "pattern": "a"},
        {"name": "x", "owner": "o", "pattern": "("},
        {"name": "", "owner": "o", "pattern": "a"},
        {"name": "x:{2}", "owner": "o", "pattern": "(a)"},
    ],
    ids=[
        "empty-owner",
        "pattern-does-not-compile",
        "empty-name",
        "group-the-pattern-lacks",
    ],
)
def test_a_rule_with_no_owner_or_a_broken_pattern_is_refused(
    rule: dict[str, str],
) -> None:
    with pytest.raises(ValidationError):
        roll([rule])


def test_a_class_a_writer_names_but_nobody_owns_gets_its_owner_from_the_pairs() -> None:
    """stoppage:tick-liveness on the floor board: the alarm wrote class=tick-liveness, and no ticket or lane owned it."""
    bare = classes(roll(RULES))["tick-liveness"]
    owned = classes(roll(RULES, OWNERS))["tick-liveness"]
    assert "owner" not in bare  # CONTROL: without the pair the class has none
    assert "queue-status tick unit" in owned["owner"]
    assert owned["rows"] == 1
    assert owned["lanes"] == ["silent-failure-alarm"]
    assert owned["rows_ruled"] == 0


@pytest.mark.parametrize(
    "pairs",
    [
        [{"class_name": "x", "owner": " "}],
        [
            {"class_name": "tick-liveness", "owner": "a"},
            {"class_name": "tick_liveness", "owner": "b"},
        ],
    ],
    ids=["empty-owner", "class-named-twice"],
)
def test_a_class_owner_pair_with_no_owner_or_a_repeated_class_is_refused(
    pairs: list[dict[str, str]],
) -> None:
    with pytest.raises(ValidationError):
        roll(RULES, pairs)
