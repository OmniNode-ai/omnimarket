# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Scores the seam crossing name node on a hashed keys-only fixture (exp-seampow-6803).

The fixture (v2) holds keys and flags only, no payload values. It carries, per consumer
model, the facts the field diff detector reads (``has_adapter``, ``extra_forbidden``,
per-field ``required`` and ``has_default``, taken from the real consumer model by the
field diff check's own summarizer); per crossing, the experiment's truth label and the
error types the real pydantic validator raised on the real body; and a healthy set of
dev-bus payload key sets that the consumer's validator accepted.

Three readings come from it and each has a control that fails without the code under test:

* the 197 real cross-topic crossings: 180 named as the experiment labelled them, and every
  name consistent with what the real validator raised;
* the healthy set: no name other than the clean label (false-alarm rate);
* the flags: with the consumer facts neutralised the same scorer reads worse.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from omnimarket.models.seam_crossing_name import (
    ModelSeamCrossing,
    ModelSeamCrossingNameRequest,
    ModelSeamCrossingNameVerdict,
    ModelSeamCrossingNaming,
)
from omnimarket.models.seam_field_diff import ModelSeamFieldFacts, ModelSeamModelSummary
from omnimarket.nodes.node_seam_crossing_name_compute.handlers.handler_seam_crossing_name import (
    HandlerSeamCrossingName,
)

pytestmark = pytest.mark.unit

FIXTURE = Path(__file__).parent / "fixtures" / "seampow_7d_keys.json"
FIXTURE_SHA256 = "7f5438cbeaff324ee728349a3e254970f35d99de9450bebc722eba767073d0f1"
NOMINAL_NAMING = ModelSeamCrossingNaming(
    missing_required="missing_field",
    undeclared_key="unknown_field",
    clean="matches",
)
ADAPTER_CROSSINGS = 13
HEALTHY_TOTAL = 300
HEALTHY_DISTINCT = 10


def _load_fixture() -> dict[str, Any]:
    fixture: dict[str, Any] = json.loads(FIXTURE.read_text())
    return fixture


def _summary(
    seam: dict[str, Any],
    *,
    extra_forbidden: bool | None = None,
    adapter: bool | None = None,
) -> ModelSeamModelSummary:
    """The consumer summary from the fixture's flags; the keyword overrides are controls."""
    return ModelSeamModelSummary(
        model_ref=seam["consumer_model"],
        fields={
            f["name"]: ModelSeamFieldFacts(
                type_name="any",
                required=f["required"],
                nullable=False,
                has_default=f["has_default"],
            )
            for f in seam["fields"]
        },
        nested={},
        extra_forbidden=(
            seam["extra_forbidden"] if extra_forbidden is None else extra_forbidden
        ),
        has_adapter=seam["has_adapter"] if adapter is None else adapter,
    )


def _build_request(
    fixture: dict[str, Any],
    section: str = "crossings",
    *,
    extra_forbidden: bool | None = None,
    adapter: bool | None = None,
    seam_for: dict[str, str] | None = None,
) -> ModelSeamCrossingNameRequest:
    seams = {s["seam_id"]: s for s in fixture["seams"]}
    crossings: list[ModelSeamCrossing] = []
    for c in fixture[section]:
        seam_id = (seam_for or {}).get(c["crossing_id"], c["seam_id"])
        seam = seams[seam_id]
        crossings.append(
            ModelSeamCrossing(
                crossing_id=c["crossing_id"],
                seam_id=seam_id,
                topic=seam["topic"],
                consumer=_summary(
                    seam, extra_forbidden=extra_forbidden, adapter=adapter
                ),
                body_keys=tuple(c["body_keys"]),
            )
        )
    return ModelSeamCrossingNameRequest(
        crossings=tuple(crossings), naming=NOMINAL_NAMING
    )


def _correct_count(
    verdict: ModelSeamCrossingNameVerdict, fixture: dict[str, Any]
) -> int:
    truth = {c["crossing_id"]: c["truth_class"] for c in fixture["crossings"]}
    return sum(1 for n in verdict.names if n.name == truth[n.crossing_id])


def _key_consistent(name: str, error_types: list[str]) -> bool:
    """Does the name agree with what the real validator raised on the real body?"""
    missing = "missing" in error_types
    extra = "extra_forbidden" in error_types
    if name == NOMINAL_NAMING.missing_required:
        return missing
    if name == NOMINAL_NAMING.undeclared_key:
        return extra and not missing
    return not missing and not extra


def _consistent_count(
    verdict: ModelSeamCrossingNameVerdict, fixture: dict[str, Any]
) -> int:
    types = {c["crossing_id"]: c["pydantic_error_types"] for c in fixture["crossings"]}
    return sum(
        1 for n in verdict.names if _key_consistent(n.name, types[n.crossing_id])
    )


def test_fixture_bytes_match_the_pinned_hash() -> None:
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == FIXTURE_SHA256


def test_the_fixture_holds_keys_and_flags_only() -> None:
    fixture = _load_fixture()
    assert fixture["schema"] == "seam-crossing-name-fixture/v2"
    for c in fixture["crossings"] + fixture["healthy"]:
        assert all(isinstance(k, str) for k in c["body_keys"])
    # A flag the old fixture could not reach is now present in real consumer facts.
    assert any(s["has_adapter"] for s in fixture["seams"])
    assert any(f["has_default"] for s in fixture["seams"] for f in s["fields"])
    assert any(s["extra_forbidden"] for s in fixture["seams"])


def test_the_node_names_180_of_197_crossings() -> None:
    fixture = _load_fixture()
    verdict = HandlerSeamCrossingName().handle(_build_request(fixture))
    assert len(verdict.names) == 197
    assert _correct_count(verdict, fixture) == 180


def test_per_class_readings_are_pinned() -> None:
    """Rule R fires on 118 crossings, 108 of them labelled missing_field (the other 10
    are labelled unknown_field by the experiment, though the real validator raised both
    missing and extra_forbidden on each); the key check names 72 of the 79 crossings
    rule R leaves as unknown_field, as scored."""
    fixture = _load_fixture()
    verdict = HandlerSeamCrossingName().handle(_build_request(fixture))
    truth = {c["crossing_id"]: c["truth_class"] for c in fixture["crossings"]}
    rule_r = [n for n in verdict.names if n.decided_by == "rule_r"]
    assert len(rule_r) == 118
    assert sum(1 for n in rule_r if truth[n.crossing_id] == "missing_field") == 108
    left = [n for n in verdict.names if n.decided_by == "key_check"]
    assert len(left) == 79
    assert (
        sum(
            1
            for n in left
            if n.name == "unknown_field" and truth[n.crossing_id] == "unknown_field"
        )
        == 72
    )


def test_every_name_is_consistent_with_the_real_validator() -> None:
    """The pydantic validator is the truth: on all 197 real bodies the name agrees with the
    error classes it raised, including the 10 rule R names the experiment labelled otherwise."""
    fixture = _load_fixture()
    verdict = HandlerSeamCrossingName().handle(_build_request(fixture))
    assert _consistent_count(verdict, fixture) == 197


def test_the_adapter_consumers_are_flagged_from_the_fixture_flags() -> None:
    fixture = _load_fixture()
    verdict = HandlerSeamCrossingName().handle(_build_request(fixture))
    assert verdict.adapter_advisory == ADAPTER_CROSSINGS
    assert sum(1 for n in verdict.names if n.consumer_has_adapter) == ADAPTER_CROSSINGS
    # Control: the count comes from the flag; with the flag neutralised it is zero.
    neutral = HandlerSeamCrossingName().handle(_build_request(fixture, adapter=False))
    assert neutral.adapter_advisory == 0


def test_the_key_check_step_is_load_bearing() -> None:
    fixture = _load_fixture()
    request = _build_request(fixture)
    handler_correct = _correct_count(HandlerSeamCrossingName().handle(request), fixture)
    control_correct = _rule_r_only_correct(request, fixture)
    assert control_correct < 180
    assert handler_correct - control_correct >= 70


def test_the_extra_forbidden_flag_is_load_bearing() -> None:
    """Control for the key check's use of extra_forbidden: a consumer that ignores extra
    keys cannot reject an undeclared key, so with the flag neutralised the key check names
    nothing and the score falls well under 180."""
    fixture = _load_fixture()
    verdict = HandlerSeamCrossingName().handle(
        _build_request(fixture, extra_forbidden=False)
    )
    assert _correct_count(verdict, fixture) < 180
    assert _consistent_count(verdict, fixture) < 197


def _rule_r_only_correct(
    request: ModelSeamCrossingNameRequest, fixture: dict[str, Any]
) -> int:
    truth = {c["crossing_id"]: c["truth_class"] for c in fixture["crossings"]}
    correct = 0
    for c in request.crossings:
        required = {
            n for n, f in c.consumer.fields.items() if f.required and not f.has_default
        }
        body = set(c.body_keys)
        name = "missing_field" if (required - body) else "matches"
        if name == truth[c.crossing_id]:
            correct += 1
    return correct


def test_the_scorer_is_not_vacuous() -> None:
    fixture = _load_fixture()
    request = _build_request(fixture)
    flipped = {
        c["crossing_id"]: (
            "missing_field" if c["truth_class"] != "missing_field" else "unknown_field"
        )
        for c in fixture["crossings"]
    }
    verdict = HandlerSeamCrossingName().handle(request)
    correct = sum(1 for n in verdict.names if n.name == flipped[n.crossing_id])
    assert correct < 20


def test_healthy_dev_bus_traffic_is_named_matches_with_no_false_alarm() -> None:
    """True matches: payload key sets the consumer's own validator accepted. The false-alarm
    rate (names other than the clean label, weighted by how often the key set was seen) is 0
    of 300."""
    fixture = _load_fixture()
    assert len(fixture["healthy"]) == HEALTHY_DISTINCT
    verdict = HandlerSeamCrossingName().handle(_build_request(fixture, "healthy"))
    weight = {h["crossing_id"]: h["count"] for h in fixture["healthy"]}
    assert sum(weight.values()) == HEALTHY_TOTAL
    alarms = sum(weight[n.crossing_id] for n in verdict.names if n.name != "matches")
    assert alarms == 0
    # Every healthy name rests on the clean label, decided by the key check.
    assert {n.decided_by for n in verdict.names} == {"key_check"}


def test_the_false_alarm_reading_is_not_vacuous() -> None:
    """Control: the same healthy key sets read against another seam's consumer are not clean,
    so the reading above can fail."""
    fixture = _load_fixture()
    healthy = fixture["healthy"]
    seam_ids = sorted({h["seam_id"] for h in healthy})
    other = {
        h["crossing_id"]: next(s for s in seam_ids if s != h["seam_id"])
        for h in healthy
    }
    verdict = HandlerSeamCrossingName().handle(
        _build_request(fixture, "healthy", seam_for=other)
    )
    assert sum(1 for n in verdict.names if n.name != "matches") >= HEALTHY_DISTINCT // 2
