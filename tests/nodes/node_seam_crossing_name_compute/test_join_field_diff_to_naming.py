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
    ModelSeamCrossingNameVerdict,
    ModelSeamCrossingNaming,
)
from omnimarket.models.seam_field_diff import (
    EnumSeamFieldIssue,
    ModelSeamFieldDiffRequest,
    ModelSeamFieldDiffVerdict,
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
        extra_forbidden=True,
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
    assert any(
        f.issue is EnumSeamFieldIssue.EXTRA_FIELD_REJECTED and f.field == "extra"
        for f in diff.findings
    )
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


def _summary(
    fields: dict[str, ModelSeamFieldFacts],
    *,
    extra_forbidden: bool = False,
    has_adapter: bool = False,
    ref: str = "mod.C",
) -> ModelSeamModelSummary:
    return ModelSeamModelSummary(
        model_ref=ref,
        fields=fields,
        nested={},
        extra_forbidden=extra_forbidden,
        has_adapter=has_adapter,
    )


def _diff(
    producer: ModelSeamModelSummary, consumer: ModelSeamModelSummary
) -> ModelSeamFieldDiffVerdict:
    pair = ModelSeamPairFacts(
        topic="t",
        producer_contract="p.yaml",
        consumer_contract="c.yaml",
        producer_rule="r1",
        consumer_rule="r2",
        producer=producer,
        consumer=consumer,
    )
    return HandlerSeamFieldDiff().handle(_req((pair,)))


def _name(
    consumer: ModelSeamModelSummary, body_keys: tuple[str, ...]
) -> ModelSeamCrossingNameVerdict:
    crossing = ModelSeamCrossing(
        crossing_id="c",
        seam_id="s",
        topic="t",
        consumer=consumer,
        body_keys=body_keys,
    )
    return HandlerSeamCrossingName().handle(
        ModelSeamCrossingNameRequest(crossings=(crossing,), naming=NAMING)
    )


def test_a_consumer_that_ignores_extra_keys_is_clean_for_both_nodes() -> None:
    """extra_forbidden False: the detector reports no rejected extra and the node names clean."""
    consumer = _summary({"a": _field()}, extra_forbidden=False)
    producer = _summary({"a": _field(), "extra": _field()}, ref="mod.P")
    diff = _diff(producer, consumer)
    assert not any(
        f.issue is EnumSeamFieldIssue.EXTRA_FIELD_REJECTED for f in diff.findings
    )
    assert _name(consumer, ("a", "extra")).names[0].name == "matches"


def test_a_defaulted_required_field_is_not_missing_for_either_node() -> None:
    """required True with has_default True: neither node reports it absent."""
    consumer = _summary({"a": _field(), "d": _field(required=True, has_default=True)})
    producer = _summary({"a": _field()}, ref="mod.P")
    diff = _diff(producer, consumer)
    assert not any(f.issue is EnumSeamFieldIssue.MISSING_FIELD for f in diff.findings)
    assert _name(consumer, ("a",)).names[0].name == "matches"
    # Control: the same field without a default is reported absent by both.
    strict = _summary({"a": _field(), "d": _field(required=True, has_default=False)})
    assert any(
        f.issue is EnumSeamFieldIssue.MISSING_FIELD and f.field == "d"
        for f in _diff(producer, strict).findings
    )
    assert _name(strict, ("a",)).names[0].name == "missing_field"


def test_an_adapter_consumer_is_skipped_by_the_detector_and_flagged_by_the_node() -> (
    None
):
    """has_adapter True: the detector skips the pair; the node still names the crossing but
    marks the name advisory, because the adapter may supply or reshape the declared fields."""
    consumer = _summary({"a": _field(), "need": _field()}, has_adapter=True)
    producer = _summary({"a": _field()}, ref="mod.P")
    diff = _diff(producer, consumer)
    assert diff.pairs_compared == 0
    assert len(diff.adapter_skipped) == 1
    verdict = _name(consumer, ("a",))
    assert verdict.names[0].name == "missing_field"
    assert verdict.names[0].consumer_has_adapter is True
    assert verdict.adapter_advisory == 1


def test_the_consumer_summary_shape_is_pinned() -> None:
    """Both nodes read these consumer facts; a change to the shape must fail here."""
    assert set(ModelSeamModelSummary.model_fields.keys()) == {
        "model_ref",
        "fields",
        "nested",
        "extra_forbidden",
        "has_adapter",
    }
