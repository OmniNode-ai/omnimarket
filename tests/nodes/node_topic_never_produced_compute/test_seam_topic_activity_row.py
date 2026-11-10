# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam: the never-produced evidence reads the topic-activity projection row (OMN-23614).

node_projection_topic_activity folds broker-owned offsets into one ModelTopicActivityRow
per topic. The listing node takes its two offset facts from that row, and takes the
partition count and the consumer groups from the broker's own describe, which the row does
not carry. This test fails when either side changes shape: a renamed or retyped row field,
a renamed evidence field, or a row that starts carrying groups or partitions (then the
listing's source must be re-wired, not left reading a second copy).
"""

from datetime import UTC, datetime

import pytest

from omnimarket.nodes.node_projection_topic_activity.models.enum_topic_activity_state import (
    EnumTopicActivityState,
)
from omnimarket.nodes.node_projection_topic_activity.models.model_topic_activity_row import (
    ModelTopicActivityRow,
)
from omnimarket.nodes.node_topic_never_produced_compute import (
    NodeTopicNeverProducedCompute,
)
from omnimarket.nodes.node_topic_never_produced_compute.models.model_topic_never_produced import (
    ModelNeverProducedRequest,
    ModelNeverProducedRule,
    ModelTopicEvidence,
)

pytestmark = pytest.mark.unit

# evidence field -> the activity row field it is read from
ROW_SOURCE = {
    "high_watermark_total": "high_watermark_total",
    "records_in_retention": "retained_messages",
}
RULE = ModelNeverProducedRule(protected_prefixes=("__",), list_declared_producers=True)


def _evidence_from(
    row: ModelTopicActivityRow, partitions: int, groups: tuple[str, ...] | None
) -> ModelTopicEvidence:
    return ModelTopicEvidence(
        name=row.topic,
        partitions=partitions,
        consumer_groups=groups,
        **{ev: getattr(row, src) for ev, src in ROW_SOURCE.items()},
    )


def _row(state: EnumTopicActivityState, **over: object) -> ModelTopicActivityRow:
    return ModelTopicActivityRow(
        topic=str(over.pop("topic", "t.seam")),
        sampled_at=datetime(2026, 11, 10, tzinfo=UTC),
        activity_state=state,
        **over,
    )


def test_both_sides_still_carry_the_fields_the_seam_reads() -> None:
    assert set(ROW_SOURCE.values()) <= set(ModelTopicActivityRow.model_fields)
    assert set(ROW_SOURCE) <= set(ModelTopicEvidence.model_fields)
    for ev, src in ROW_SOURCE.items():
        assert (
            ModelTopicEvidence.model_fields[ev].annotation
            == ModelTopicActivityRow.model_fields[src].annotation
        )


def test_the_row_does_not_carry_groups_or_partitions() -> None:
    assert not {"consumer_groups", "partitions", "partition_count"} & set(
        ModelTopicActivityRow.model_fields
    )


def test_a_row_with_zero_offsets_lists_and_an_unread_row_does_not() -> None:
    state = next(iter(EnumTopicActivityState))
    zero = _row(state, high_watermark_total=0, retained_messages=0)
    unread = _row(state, topic="t.unread")
    produced = _row(
        state, topic="t.produced", high_watermark_total=5, retained_messages=0
    )
    report = NodeTopicNeverProducedCompute().handle(
        ModelNeverProducedRequest(
            rule=RULE,
            topics=tuple(_evidence_from(r, 1, ()) for r in (zero, unread, produced)),
        )
    )
    assert [t.name for t in report.listed] == ["t.seam"]
    assert {(n.name, n.reason) for n in report.not_listed} == {
        ("t.unread", "evidence_unread"),
        ("t.produced", "produced"),
    }
    assert report.deletes_issued == 0
