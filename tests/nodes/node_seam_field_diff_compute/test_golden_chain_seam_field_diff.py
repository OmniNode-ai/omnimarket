# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: clean verdicts for compatible producer/consumer shapes (OMN-24121)."""

from __future__ import annotations

import pytest

from omnimarket.models.seam_field_diff import (
    ModelSeamFieldDiffRequest,
    ModelSeamFieldFacts,
    ModelSeamModelSummary,
    ModelSeamPairFacts,
)
from omnimarket.nodes.node_seam_field_diff_compute.handlers.handler_seam_field_diff import (
    HandlerSeamFieldDiff,
)

pytestmark = pytest.mark.unit


def _field(
    type_name: str = "string",
    required: bool = True,
    nullable: bool = False,
    has_default: bool = False,
) -> ModelSeamFieldFacts:
    return ModelSeamFieldFacts(
        type_name=type_name,
        required=required,
        nullable=nullable,
        has_default=has_default,
    )


def _pair(
    producer_fields: dict[str, ModelSeamFieldFacts],
    consumer_fields: dict[str, ModelSeamFieldFacts],
    producer_ref: str = "mod.P",
    consumer_ref: str = "mod.C",
    producer_extra_forbidden: bool = False,
    consumer_extra_forbidden: bool = False,
    producer_nested: dict[str, dict[str, ModelSeamFieldFacts]] | None = None,
    consumer_nested: dict[str, dict[str, ModelSeamFieldFacts]] | None = None,
) -> ModelSeamPairFacts:
    return ModelSeamPairFacts(
        topic="t",
        producer_contract="p.yaml",
        consumer_contract="c.yaml",
        producer_rule="r1",
        consumer_rule="r2",
        producer=ModelSeamModelSummary(
            model_ref=producer_ref,
            fields=producer_fields,
            nested=producer_nested or {},
            extra_forbidden=producer_extra_forbidden,
            has_adapter=False,
        ),
        consumer=ModelSeamModelSummary(
            model_ref=consumer_ref,
            fields=consumer_fields,
            nested=consumer_nested or {},
            extra_forbidden=consumer_extra_forbidden,
            has_adapter=False,
        ),
    )


def _req(
    pairs: tuple[ModelSeamPairFacts, ...], depth: int = 3
) -> ModelSeamFieldDiffRequest:
    return ModelSeamFieldDiffRequest(
        head_pairs=pairs,
        base_finding_keys=(),
        nested_depth_limit=depth,
    )


def test_consumer_subset_of_producer_is_clean() -> None:
    pair = _pair(
        producer_fields={"a": _field(), "b": _field(), "c": _field()},
        consumer_fields={"a": _field(), "b": _field()},
    )
    v = HandlerSeamFieldDiff().handle(_req((pair,)))
    assert v.status == "clean"
    assert v.findings == ()
    assert v.pairs_compared == 1


def test_consumer_required_field_with_default_producer_lacks_is_clean() -> None:
    pair = _pair(
        producer_fields={"a": _field()},
        consumer_fields={"a": _field(), "b": _field(has_default=True)},
    )
    v = HandlerSeamFieldDiff().handle(_req((pair,)))
    assert v.status == "clean"
    assert v.findings == ()


def test_nested_ref_models_same_shape_different_name_is_clean() -> None:
    nested_p = {"Inner": {"x": _field("integer")}}
    nested_c = {"Inner2": {"x": _field("integer")}}
    pair = _pair(
        producer_fields={"n": _field("ref:Inner")},
        consumer_fields={"n": _field("ref:Inner2")},
        producer_nested=nested_p,
        consumer_nested=nested_c,
    )
    v = HandlerSeamFieldDiff().handle(_req((pair,)))
    assert v.status == "clean"
    assert v.findings == ()


def test_enum_subset_into_string_is_clean() -> None:
    pair = _pair(
        producer_fields={"s": _field("enum:a,b")},
        consumer_fields={"s": _field("string")},
    )
    v = HandlerSeamFieldDiff().handle(_req((pair,)))
    assert v.status == "clean"
    assert v.findings == ()


def test_integer_into_number_is_clean() -> None:
    pair = _pair(
        producer_fields={"n": _field("integer")},
        consumer_fields={"n": _field("number")},
    )
    v = HandlerSeamFieldDiff().handle(_req((pair,)))
    assert v.status == "clean"
    assert v.findings == ()


def test_verdict_is_deterministic() -> None:
    pair = _pair(
        producer_fields={"a": _field(), "b": _field("integer")},
        consumer_fields={"a": _field(), "b": _field("string")},
    )
    h = HandlerSeamFieldDiff()
    v1 = h.handle(_req((pair,)))
    v2 = h.handle(_req((pair,)))
    assert v1 == v2
