# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain: each issue type, base-key control, adapter skip, nested depth (OMN-24121)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.models.seam_field_diff import (
    EnumSeamFieldIssue,
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
    consumer_has_adapter: bool = False,
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
            has_adapter=consumer_has_adapter,
        ),
    )


def _req(
    pairs: tuple[ModelSeamPairFacts, ...],
    base_keys: tuple[str, ...] = (),
    depth: int = 3,
) -> ModelSeamFieldDiffRequest:
    return ModelSeamFieldDiffRequest(
        head_pairs=pairs,
        base_finding_keys=base_keys,
        nested_depth_limit=depth,
    )


def test_missing_field_is_refused() -> None:
    pair = _pair(
        producer_fields={"a": _field()},
        consumer_fields={"a": _field(), "b": _field()},
    )
    v = HandlerSeamFieldDiff().handle(_req((pair,)))
    assert v.status == "refused"
    assert any(
        f.issue is EnumSeamFieldIssue.MISSING_FIELD and f.field == "b"
        for f in v.findings
    )


def test_type_mismatch_is_refused() -> None:
    pair = _pair(
        producer_fields={"a": _field("integer")},
        consumer_fields={"a": _field("string")},
    )
    v = HandlerSeamFieldDiff().handle(_req((pair,)))
    assert v.status == "refused"
    assert any(
        f.issue is EnumSeamFieldIssue.TYPE_MISMATCH and f.field == "a"
        for f in v.findings
    )


def test_optional_vs_required_is_refused() -> None:
    pair = _pair(
        producer_fields={"a": _field(required=False)},
        consumer_fields={"a": _field(required=True)},
    )
    v = HandlerSeamFieldDiff().handle(_req((pair,)))
    assert v.status == "refused"
    assert any(
        f.issue is EnumSeamFieldIssue.OPTIONAL_VS_REQUIRED and f.field == "a"
        for f in v.findings
    )


def test_extra_field_rejected_is_refused() -> None:
    pair = _pair(
        producer_fields={"a": _field(), "b": _field()},
        consumer_fields={"a": _field()},
        consumer_extra_forbidden=True,
    )
    v = HandlerSeamFieldDiff().handle(_req((pair,)))
    assert v.status == "refused"
    assert any(
        f.issue is EnumSeamFieldIssue.EXTRA_FIELD_REJECTED and f.field == "b"
        for f in v.findings
    )


def test_base_finding_keys_suppress_new_findings() -> None:
    pair = _pair(
        producer_fields={"a": _field("integer")},
        consumer_fields={"a": _field("string")},
    )
    h = HandlerSeamFieldDiff()
    v_empty = h.handle(_req((pair,)))
    assert v_empty.status == "refused"
    keys = tuple(f.key for f in v_empty.findings)
    v_base = h.handle(_req((pair,), base_keys=keys))
    assert v_base.status == "clean"
    assert v_base.new_findings == ()


def test_adapter_consumer_is_skipped() -> None:
    pair = _pair(
        producer_fields={"a": _field("integer")},
        consumer_fields={"a": _field("string")},
        consumer_has_adapter=True,
    )
    v = HandlerSeamFieldDiff().handle(_req((pair,)))
    assert pair.pair_key in v.adapter_skipped
    assert v.findings == ()
    assert v.pairs_compared == 0


def test_same_model_ref_is_skipped() -> None:
    pair = _pair(
        producer_fields={"a": _field("integer")},
        consumer_fields={"a": _field("string")},
        producer_ref="mod.Same",
        consumer_ref="mod.Same",
    )
    v = HandlerSeamFieldDiff().handle(_req((pair,)))
    assert v.pairs_compared == 0
    assert v.findings == ()


def test_nested_depth_limit_changes_verdict() -> None:
    inner_p = {"x": _field("integer")}
    inner_c = {"x": _field("string")}
    pair = _pair(
        producer_fields={"n": _field("ref:Inner")},
        consumer_fields={"n": _field("ref:Inner")},
        producer_nested={"Inner": inner_p},
        consumer_nested={"Inner": inner_c},
    )
    h = HandlerSeamFieldDiff()
    v1 = h.handle(_req((pair,), depth=1))
    v3 = h.handle(_req((pair,), depth=3))
    assert v1.status == "clean"
    assert v3.status == "refused"


def test_nested_depth_limit_zero_is_rejected() -> None:
    pair = _pair(
        producer_fields={"a": _field()},
        consumer_fields={"a": _field()},
    )
    with pytest.raises(ValidationError):
        ModelSeamFieldDiffRequest(
            head_pairs=(pair,),
            base_finding_keys=(),
            nested_depth_limit=0,
        )
