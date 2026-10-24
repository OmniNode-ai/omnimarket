"""OMN-19448: quarantine lets later recorded attempt evidence reach the row."""

from __future__ import annotations

import pytest

from tests.test_projection_error_classification_omn13634 import (
    DELEGATION_DLQ_TOPIC,
    _CommitRecordingConsumer,
    _wrapped_msg,
)
from tests.unit.projection.test_delegation_data_source_experiment_omn19448 import (
    recorded_terminal,
    recording_runner,
)
from tests.unit.projection.test_dlq_oversize_poison_omn19448 import (
    bounded_publisher,
    oversized_poison,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("traffic_class", "expected"), [(None, "real"), ("synthetic", "experiment")]
)
async def test_recorded_terminal_after_quarantine(
    traffic_class: str | None, expected: str
) -> None:
    published, publish = bounded_publisher()
    runner, rows = recording_runner(publish)
    consumer = _CommitRecordingConsumer()
    runner._consumer = consumer
    topic = runner._topic_delegate_skill_completed
    await runner._handle_message(_wrapped_msg(topic, 41, oversized_poison()))
    assert [list(commit.values()) for commit in consumer.commits] == [[42]]
    terminal = recorded_terminal(traffic_class)
    await runner._handle_message(_wrapped_msg(topic, 42, terminal))
    assert len([topic for topic, _ in published if topic == DELEGATION_DLQ_TOPIC]) == 1
    assert [list(commit.values()) for commit in consumer.commits] == [[42], [43]]
    (row,) = rows
    assert row["correlation_id"] == terminal["correlation_id"]
    assert row["backend_id"] == "local-heavy-reasoning"
    assert row["host"] == "lab-host-a.example"
    assert row["answering_backend"]
    assert row["attempt_history"]
    assert row["data_source"] == expected
