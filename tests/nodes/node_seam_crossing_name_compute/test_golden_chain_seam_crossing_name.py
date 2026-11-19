# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: seam crossing name handler (rule R, key check, clean)."""

from __future__ import annotations

import pytest

from omnimarket.models.seam_crossing_name import (
    ModelSeamCrossing,
    ModelSeamCrossingNameRequest,
    ModelSeamCrossingNaming,
)
from omnimarket.models.seam_field_diff import (
    ModelSeamFieldFacts,
    ModelSeamModelSummary,
)
from omnimarket.nodes.node_seam_crossing_name_compute.handlers.handler_seam_crossing_name import (
    HandlerSeamCrossingName,
)

pytestmark = pytest.mark.unit

NAMING = ModelSeamCrossingNaming(
    missing_required="missing_field",
    undeclared_key="unknown_field",
    clean="matches",
)


def _facts(required: bool = True) -> ModelSeamFieldFacts:
    return ModelSeamFieldFacts(
        type_name="string",
        required=required,
        nullable=False,
        has_default=False,
    )


def _consumer(fields: dict[str, ModelSeamFieldFacts]) -> ModelSeamModelSummary:
    return ModelSeamModelSummary(
        model_ref="mod.C",
        fields=fields,
        nested={},
        extra_forbidden=True,
        has_adapter=False,
    )


def _crossing(
    crossing_id: str,
    consumer: ModelSeamModelSummary,
    body_keys: tuple[str, ...],
) -> ModelSeamCrossing:
    return ModelSeamCrossing(
        crossing_id=crossing_id,
        seam_id="seam-1",
        topic="topic-1",
        consumer=consumer,
        body_keys=body_keys,
    )


def _req(crossings: tuple[ModelSeamCrossing, ...]) -> ModelSeamCrossingNameRequest:
    return ModelSeamCrossingNameRequest(crossings=crossings, naming=NAMING)


def test_rule_r_fires_when_required_field_absent() -> None:
    consumer = _consumer({"a": _facts(True), "b": _facts(False)})
    crossing = _crossing("c1", consumer, ("b", "x"))
    v = HandlerSeamCrossingName().handle(_req((crossing,)))
    assert v.names[0].name == "missing_field"
    assert v.names[0].fields == ("a",)
    assert v.names[0].decided_by.value == "rule_r"


def test_rule_r_beats_undeclared_key() -> None:
    consumer = _consumer({"a": _facts(True), "b": _facts(False)})
    crossing = _crossing("c1", consumer, ("b", "x"))
    v = HandlerSeamCrossingName().handle(_req((crossing,)))
    assert v.names[0].name == "missing_field"
    assert v.names[0].decided_by.value == "rule_r"


def test_key_check_names_undeclared_key() -> None:
    consumer = _consumer({"a": _facts(True), "b": _facts(False)})
    crossing = _crossing("c1", consumer, ("a", "b", "x"))
    v = HandlerSeamCrossingName().handle(_req((crossing,)))
    assert v.names[0].name == "unknown_field"
    assert v.names[0].fields == ("x",)
    assert v.names[0].decided_by.value == "key_check"


def test_clean_when_only_declared_keys() -> None:
    consumer = _consumer({"a": _facts(True), "b": _facts(False)})
    crossing = _crossing("c1", consumer, ("a",))
    v = HandlerSeamCrossingName().handle(_req((crossing,)))
    assert v.names[0].name == "matches"
    assert v.names[0].fields == ()
    assert v.names[0].decided_by.value == "key_check"


def test_optional_field_absent_does_not_fire_rule_r() -> None:
    consumer = _consumer({"a": _facts(True), "b": _facts(False)})
    crossing = _crossing("c1", consumer, ("a",))
    v = HandlerSeamCrossingName().handle(_req((crossing,)))
    assert v.names[0].name == "matches"


def test_verdict_preserves_order_and_counts() -> None:
    consumer = _consumer({"a": _facts(True), "b": _facts(False)})
    c1 = _crossing("c1", consumer, ("a",))
    c2 = _crossing("c2", consumer, ("a", "x"))
    v = HandlerSeamCrossingName().handle(_req((c1, c2)))
    assert [n.crossing_id for n in v.names] == ["c1", "c2"]
    assert v.counts == {"matches": 1, "unknown_field": 1}
