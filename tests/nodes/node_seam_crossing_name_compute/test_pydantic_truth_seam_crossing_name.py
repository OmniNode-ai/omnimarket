# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The pydantic validator is the truth for what a consumer accepts.

The naming node decides a crossing's name from the consumer's declared fields
(``ModelSeamModelSummary``). These cases check that decision against the real
pydantic validator, using the consumer summary produced by the field diff
check's own summarizer (``summarize_model``). Where the consumer accepts a body
the node must name it clean; where it rejects a required or undeclared key the
node must name the matching rule.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError, model_validator

from omnimarket.models.seam_crossing_name import (
    ModelSeamCrossing,
    ModelSeamCrossingName,
    ModelSeamCrossingNameRequest,
    ModelSeamCrossingNameVerdict,
    ModelSeamCrossingNaming,
)
from omnimarket.models.seam_field_diff import (
    ModelSeamFieldFacts,
    ModelSeamModelSummary,
)
from omnimarket.nodes.node_seam_crossing_name_compute.handlers.handler_seam_crossing_name import (
    HandlerSeamCrossingName,
)
from omnimarket.nodes.node_seam_field_diff_check_effect.handlers.pair_loader import (
    summarize_model,
)

pytestmark = pytest.mark.unit

NAMING = ModelSeamCrossingNaming(
    missing_required="missing_field",
    undeclared_key="unknown_field",
    clean="matches",
)


class ModelStrict(BaseModel):
    model_config = {"extra": "forbid"}

    a: str
    b: str = "x"


class ModelLoose(BaseModel):
    model_config = {"extra": "ignore"}

    a: str
    b: str = "x"


class ModelAdapter(BaseModel):
    model_config = {"extra": "forbid"}

    a: str
    b: str = "x"

    @model_validator(mode="before")
    @classmethod
    def _alias_a(cls, data: object) -> object:
        if isinstance(data, dict) and "alias_a" in data and "a" not in data:
            data = dict(data)
            data["a"] = data.pop("alias_a")
        return data


def _types(model: type[BaseModel], keys: tuple[str, ...]) -> set[str]:
    try:
        model.model_validate(dict.fromkeys(keys, "x"))
    except ValidationError as err:
        return {e["type"] for e in err.errors()}
    return set()


def _name(
    model: type[BaseModel], keys: tuple[str, ...]
) -> tuple[ModelSeamCrossingName, ModelSeamCrossingNameVerdict]:
    consumer = ModelSeamModelSummary.model_validate(summarize_model(model))
    crossing = ModelSeamCrossing(
        crossing_id="c",
        seam_id="s",
        topic="t",
        consumer=consumer,
        body_keys=keys,
    )
    verdict = HandlerSeamCrossingName().handle(
        ModelSeamCrossingNameRequest(crossings=(crossing,), naming=NAMING)
    )
    return verdict.names[0], verdict


def test_strict_required_present_is_clean() -> None:
    assert _types(ModelStrict, ("a",)) == set()
    name, _ = _name(ModelStrict, ("a",))
    assert name.name == "matches"


def test_strict_missing_required_is_named() -> None:
    assert "missing" in _types(ModelStrict, ("b",))
    name, _ = _name(ModelStrict, ("b",))
    assert name.name == "missing_field"
    assert name.fields == ("a",)
    assert name.decided_by == "rule_r"


def test_strict_undeclared_key_is_named() -> None:
    assert "extra_forbidden" in _types(ModelStrict, ("a", "zzz"))
    name, _ = _name(ModelStrict, ("a", "zzz"))
    assert name.name == "unknown_field"
    assert name.fields == ("zzz",)


def test_loose_undeclared_key_is_ignored() -> None:
    assert _types(ModelLoose, ("a", "zzz")) == set()
    name, _ = _name(ModelLoose, ("a", "zzz"))
    assert name.name == "matches"


def test_adapter_supplies_the_required_field() -> None:
    assert _types(ModelAdapter, ("alias_a",)) == set()
    name, verdict = _name(ModelAdapter, ("alias_a",))
    assert name.name == "missing_field"
    assert name.consumer_has_adapter is True
    assert verdict.adapter_advisory == 1
    name_s, verdict_s = _name(ModelStrict, ("alias_a",))
    assert name_s.consumer_has_adapter is False
    assert verdict_s.adapter_advisory == 0


def test_defaulted_required_field_is_not_missing() -> None:
    consumer = ModelSeamModelSummary(
        model_ref="mod.C",
        fields={
            "a": ModelSeamFieldFacts(
                type_name="string",
                required=True,
                nullable=False,
                has_default=True,
            )
        },
        nested={},
        extra_forbidden=False,
        has_adapter=False,
    )
    crossing = ModelSeamCrossing(
        crossing_id="c",
        seam_id="s",
        topic="t",
        consumer=consumer,
        body_keys=(),
    )
    verdict = HandlerSeamCrossingName().handle(
        ModelSeamCrossingNameRequest(crossings=(crossing,), naming=NAMING)
    )
    assert verdict.names[0].name == "matches"


def test_pydantic_and_node_agree_over_all_cases() -> None:
    cases = [
        (ModelStrict, ("a",), "matches"),
        (ModelStrict, ("b",), "missing_field"),
        (ModelStrict, ("a", "zzz"), "unknown_field"),
        (ModelLoose, ("a", "zzz"), "matches"),
    ]
    for model, keys, expected in cases:
        types = _types(model, keys)
        name, _ = _name(model, keys)
        assert name.name == expected
        if expected == "matches":
            assert types == set()
        if expected == "missing_field":
            assert "missing" in types
        if expected == "unknown_field":
            assert "extra_forbidden" in types
