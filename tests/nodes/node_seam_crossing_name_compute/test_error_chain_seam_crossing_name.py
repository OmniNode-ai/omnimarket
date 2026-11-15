# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain: seam crossing name handler validation and controls."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.models.seam_crossing_name import (
    EnumSeamCrossingDecidedBy,
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


def test_duplicate_crossing_id_raises() -> None:
    consumer = _consumer({"a": _facts()})
    c1 = _crossing("dup", consumer, ("a",))
    c2 = _crossing("dup", consumer, ("a",))
    with pytest.raises(ValidationError):
        _req((c1, c2))


def test_empty_label_raises() -> None:
    with pytest.raises(ValidationError):
        ModelSeamCrossingNaming(
            missing_required="",
            undeclared_key="unknown_field",
            clean="matches",
        )


def test_extra_field_raises() -> None:
    with pytest.raises(ValidationError):
        ModelSeamCrossingNaming(
            missing_required="missing_field",
            undeclared_key="unknown_field",
            clean="matches",
            extra="x",
        )


def test_empty_crossings_gives_empty_verdict() -> None:
    v = HandlerSeamCrossingName().handle(_req(()))
    assert v.names == ()
    assert v.counts == {}


def test_control_key_check_step() -> None:
    consumer = _consumer({"a": _facts(True), "b": _facts(False)})
    crossing = _crossing("c1", consumer, ("a", "b", "x"))
    v = HandlerSeamCrossingName().handle(_req((crossing,)))
    assert v.names[0].name == "unknown_field"
    assert v.names[0].name != "matches"


def test_control_rule_r_step() -> None:
    consumer = _consumer({"a": _facts(True), "b": _facts(False)})
    crossing = _crossing("c1", consumer, ("b", "x"))
    v = HandlerSeamCrossingName().handle(_req((crossing,)))
    assert v.names[0].name == "missing_field"
    assert v.names[0].decided_by is EnumSeamCrossingDecidedBy.RULE_R
