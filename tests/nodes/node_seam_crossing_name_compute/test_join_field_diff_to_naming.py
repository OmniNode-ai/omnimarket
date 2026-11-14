# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seam between the field diff detector and the naming node, both on one declared side.

The field diff detector (``node_seam_field_diff_compute``) and the naming node
(``node_seam_crossing_name_compute``) both consume the same ``ModelSeamModelSummary``
shape for the consumer's declared fields. These cases drive both handlers on the same
consumer summary and assert they agree on the field that decides the crossing.
"""

from __future__ import annotations

import pytest

from omnimarket.models.seam_crossing_name import (
    ModelSeamCrossing,
    ModelSeamCrossingNameRequest,
    ModelSeamCrossingNaming,
)
from omnimarket.models.seam_field_diff import (
    EnumSeamFieldIssue,
    ModelSeamFieldDiffRequest,
    ModelSeamFieldFacts,
    ModelSeamModelSummary,
    ModelSeamPairFacts,
)
from omnimarket.nodes.node_seam_crossing_name_compute.handlers.handler_seam_crossing_name import (
    HandlerSeamCrossingName,
)
from omnimarket.nodes.node_seam_field_diff_compute.handlers.handler_seam_field_diff import (
    HandlerSeamFieldDiff,
)

pytestmark = pytest.mark.unit

NAMING = ModelSeamCrossingNaming(
    missing_required="missing_field",
    undeclared_key="unknown_field",
    clean="matches",
)


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
            nested={},
            extra_forbidden=False,
            has_adapter=False,
        ),
        consumer=ModelSeamModelSummary(
            model_ref=consumer_ref,
            fields=consumer_fields,
            nested={},
            extra_forbidden=False,
            has_adapter=False,
        ),
    )


def _req(pairs: tuple[ModelSeamPairFacts, ...]) -> ModelSeamFieldDiffRequest:
    return ModelSeamFieldDiffRequest(
        head_pairs=pairs,
        base_finding_keys=(),
        nested_depth_limit=3,
    )


def test_a_missing_required_field_is_agreed_by_both_nodes() -> None:
    consumer = ModelSeamModelSummary(
        model_ref="mod.C",
        fields={"a": _field(), "need": _field()},
        nested={},
        extra_forbidden=False,
        has_adapter=False,
    )
    producer = ModelSeamModelSummary(
        model_ref="mod.P",
        fields={"a": _field()},
        nested={},
        extra_forbidden=False,
        has_adapter=False,
    )
    pair = ModelSeamPairFacts(
        topic="t",
        producer_contract="p.yaml",
        consumer_contract="c.yaml",
        producer_rule="r1",
        consumer_rule="r2",
        producer=producer,
        consumer=consumer,
    )
    diff = HandlerSeamFieldDiff().handle(_req((pair,)))
    assert any(
        f.issue is EnumSeamFieldIssue.MISSING_FIELD and f.field == "need"
        for f in diff.findings
    )
    crossing = ModelSeamCrossing(
        crossing_id="c1",
        seam_id="s1",
        topic="t",
        consumer=consumer,
        body_keys=("a",),
    )
    verdict = HandlerSeamCrossingName().handle(
        ModelSeamCrossingNameRequest(crossings=(crossing,), naming=NAMING)
    )
    name = verdict.names[0]
    assert name.name == "missing_field"
    assert name.fields == ("need",)


def test_an_undeclared_key_is_agreed_by_both_nodes() -> None:
    consumer = ModelSeamModelSummary(
        model_ref="mod.C",
        fields={"a": _field()},
        nested={},
        extra_forbidden=False,
        has_adapter=False,
    )
    producer = ModelSeamModelSummary(
        model_ref="mod.P",
        fields={"a": _field(), "extra": _field()},
        nested={},
        extra_forbidden=False,
        has_adapter=False,
    )
    pair = ModelSeamPairFacts(
        topic="t",
        producer_contract="p.yaml",
        consumer_contract="c.yaml",
        producer_rule="r1",
        consumer_rule="r2",
        producer=producer,
        consumer=consumer,
    )
    diff = HandlerSeamFieldDiff().handle(_req((pair,)))
    assert not any(f.issue is EnumSeamFieldIssue.MISSING_FIELD for f in diff.findings)
    crossing = ModelSeamCrossing(
        crossing_id="c2",
        seam_id="s1",
        topic="t",
        consumer=consumer,
        body_keys=("a", "extra"),
    )
    verdict = HandlerSeamCrossingName().handle(
        ModelSeamCrossingNameRequest(crossings=(crossing,), naming=NAMING)
    )
    name = verdict.names[0]
    assert name.name == "unknown_field"
    assert name.fields == ("extra",)


def test_the_declared_side_shape_is_pinned() -> None:
    assert (
        ModelSeamCrossing.model_fields["consumer"].annotation is ModelSeamModelSummary
    )
    assert set(ModelSeamFieldFacts.model_fields.keys()) == {
        "type_name",
        "required",
        "nullable",
        "has_default",
    }
