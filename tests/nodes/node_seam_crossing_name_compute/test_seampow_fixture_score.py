# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Scores the seam crossing name node on a hashed 7-day fixture (exp-seampow-6803).

The fixture is hashed before any call, so the measurement is pinned: rule R plus the
deterministic key check named 180 of 197 real crossings. These cases re-derive that
number from the fixture and guard it against a vacuous scorer.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from omnimarket.models.seam_crossing_name import (
    ModelSeamCrossing,
    ModelSeamCrossingNameRequest,
    ModelSeamCrossingNaming,
)
from omnimarket.models.seam_field_diff import ModelSeamFieldFacts, ModelSeamModelSummary
from omnimarket.nodes.node_seam_crossing_name_compute.handlers.handler_seam_crossing_name import (
    HandlerSeamCrossingName,
)

pytestmark = pytest.mark.unit

FIXTURE = Path(__file__).parent / "fixtures" / "seampow_7d_keys.json"
FIXTURE_SHA256 = "dcd575c354a09d12f1a4d04cd31823f8b787cd82b8525dae90bfcedb0e1a5a3d"
NOMINAL_NAMING = ModelSeamCrossingNaming(
    missing_required="missing_field",
    undeclared_key="unknown_field",
    clean="matches",
)


def _load_fixture() -> dict[str, object]:
    return json.loads(FIXTURE.read_text())


def _build_request(fixture: dict[str, object]) -> ModelSeamCrossingNameRequest:
    seams = {s["seam_id"]: s for s in fixture["seams"]}
    crossings: list[ModelSeamCrossing] = []
    for c in fixture["crossings"]:
        seam = seams[c["seam_id"]]
        fields = {
            f["name"]: ModelSeamFieldFacts(
                type_name="any",
                required=f["required"],
                nullable=False,
                has_default=not f["required"],
            )
            for f in seam["fields"]
        }
        crossings.append(
            ModelSeamCrossing(
                crossing_id=c["crossing_id"],
                seam_id=c["seam_id"],
                topic=seam["topic"],
                consumer=ModelSeamModelSummary(
                    model_ref=seam["consumer_model"],
                    fields=fields,
                    nested={},
                    extra_forbidden=False,
                    has_adapter=False,
                ),
                body_keys=tuple(c["body_keys"]),
            )
        )
    return ModelSeamCrossingNameRequest(
        crossings=tuple(crossings), naming=NOMINAL_NAMING
    )


def _correct_count(verdict, fixture: dict[str, object]) -> int:
    truth = {c["crossing_id"]: c["truth_class"] for c in fixture["crossings"]}
    return sum(1 for n in verdict.names if n.name == truth[n.crossing_id])


def test_fixture_bytes_match_the_pinned_hash() -> None:
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == FIXTURE_SHA256


def test_the_node_names_180_of_197_crossings() -> None:
    fixture = _load_fixture()
    verdict = HandlerSeamCrossingName().handle(_build_request(fixture))
    assert len(verdict.names) == 197
    assert _correct_count(verdict, fixture) == 180


def test_per_class_readings_are_pinned() -> None:
    """Rule R fires on 118 crossings, 108 of them truly missing_field (10 where the
    consumer's before-validator supplies the declared required names); the key check
    names 72 of the 79 crossings rule R leaves as unknown_field, as scored."""
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


def test_the_key_check_step_is_load_bearing() -> None:
    fixture = _load_fixture()
    request = _build_request(fixture)
    handler_correct = _correct_count(HandlerSeamCrossingName().handle(request), fixture)
    control_correct = _rule_r_only_correct(request, fixture)
    assert control_correct < 180
    assert handler_correct - control_correct >= 70


def _rule_r_only_correct(
    request: ModelSeamCrossingNameRequest, fixture: dict[str, object]
) -> int:
    truth = {c["crossing_id"]: c["truth_class"] for c in fixture["crossings"]}
    correct = 0
    for c in request.crossings:
        required = {n for n, f in c.consumer.fields.items() if f.required}
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
