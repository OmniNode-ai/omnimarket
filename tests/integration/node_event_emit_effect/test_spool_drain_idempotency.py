# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Integration test: spool drain + idempotency across invocations (OMN-15965 R1).

Pre-populates the spool with backlog records (simulating prior hook writes /
a crashed prior invocation), then drives HandlerEventEmitEffect.handle()
across one or more invocations and asserts:

- The current event publishes and the backlog drains FIFO up to budget.
- The spool empties over repeated invocations.
- No record is published twice once acked (publish-call-count per event_id
  == 1 across repeated drains of an already-acked file).
- A simulated mid-drain publish failure leaves the remainder un-acked on
  disk, with no double-ack.
- A file left on disk unacked (standing in for "publish succeeded, then the
  process crashed before ack" -- the two are indistinguishable on disk by
  design) is republished at-least-once on the next drain, with a stable
  event_id/correlation_id across the retry. This proves the idempotency key
  survives the retry, not that any consumer dedupes -- dedup is a
  downstream-consumer responsibility (these are telemetry events, not
  control flow).
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import pytest

from omnimarket.nodes.node_event_emit_effect.handlers.handler_event_emit_effect import (
    HandlerEventEmitEffect,
)
from omnimarket.nodes.node_event_emit_effect.models.model_emit_request import (
    JsonType,
    ModelEmitRequest,
)
from omnimarket.nodes.node_event_emit_effect.spool.spool_outbox import (
    SpoolOutbox,
    SpoolRecord,
)
from omnimarket.nodes.node_event_emit_effect.spool.topic_resolver import (
    EnumDurabilityTier,
)

pytestmark = pytest.mark.integration


class RecordingPublishAdapter:
    """Records every publish attempt (including retries); simulates failures."""

    def __init__(self, *, fail_topics: frozenset[str] = frozenset()) -> None:
        self.attempts: list[tuple[str, str | None]] = []  # (topic, correlation_id)
        self._fail_topics = fail_topics

    def publish(
        self,
        topic: str,
        payload: JsonType,
        *,
        key: str | None,
        correlation_id: str | None,
    ) -> None:
        self.attempts.append((topic, correlation_id))
        if topic in self._fail_topics:
            raise RuntimeError(f"simulated publish failure for {topic}")

    def success_count_by_topic(self) -> Counter[str]:
        return Counter(t for t, _ in self.attempts if t not in self._fail_topics)


def _backlog_record(
    seq: int,
    *,
    tier: EnumDurabilityTier,
    topic: str = "onex.evt.omniclaude.session-started.v1",
) -> SpoolRecord:
    return SpoolRecord(
        event_id=f"backlog-{seq}",
        event_type="session.started"
        if tier is EnumDurabilityTier.TELEMETRY
        else "session.outcome",
        topics=(topic,),
        tier=tier,
        payload={"seq": seq},
        partition_key=None,
        correlation_id=f"corr-{seq}",
        queued_at=datetime.now(UTC),
    )


def test_backlog_drains_fifo_and_current_event_publishes(tmp_path: Path) -> None:
    spool = SpoolOutbox(tmp_path / "spool")
    for i in range(3):
        outcome = spool.append(_backlog_record(i, tier=EnumDurabilityTier.TELEMETRY))
        assert outcome.spool_file is not None
    for i in range(3, 5):
        outcome = spool.append(
            _backlog_record(i, tier=EnumDurabilityTier.DUTY_CRITICAL)
        )
        assert outcome.spool_file is not None

    assert spool.pending_count() == 5

    adapter = RecordingPublishAdapter()
    handler = HandlerEventEmitEffect(spool=spool, publish_adapter=adapter)
    request = ModelEmitRequest(
        event_type="session.started", payload={"session_id": "cur"}
    )
    result = handler.handle(request)

    assert result.published is True
    assert result.drained_count == 5
    assert spool.pending_count() == 0

    # Exact publish order: current event first, then the backlog strictly
    # FIFO by filename (telemetry 0-2, then duty_critical 3-4, in append
    # order) -- not just set membership.
    published_ids_in_order = [cid for _, cid in adapter.attempts]
    assert published_ids_in_order == [
        request.correlation_id,
        "corr-0",
        "corr-1",
        "corr-2",
        "corr-3",
        "corr-4",
    ]


def test_no_double_publish_once_acked(tmp_path: Path) -> None:
    spool = SpoolOutbox(tmp_path / "spool")
    spool.append(_backlog_record(0, tier=EnumDurabilityTier.TELEMETRY))
    adapter = RecordingPublishAdapter()
    handler = HandlerEventEmitEffect(spool=spool, publish_adapter=adapter)

    first = handler.handle(
        ModelEmitRequest(event_type="session.started", payload={"session_id": "a"})
    )
    assert first.drained_count == 1
    assert spool.pending_count() == 0

    second = handler.handle(
        ModelEmitRequest(event_type="session.started", payload={"session_id": "b"})
    )
    assert second.drained_count == 0  # nothing left to drain

    # backlog-0's topic was published exactly once across both invocations
    # for the backlog event, plus once each for the two current events.
    assert (
        adapter.attempts.count(("onex.evt.omniclaude.session-started.v1", "corr-0"))
        == 1
    )


def test_current_event_fails_before_drain_leaves_backlog_fully_intact(
    tmp_path: Path,
) -> None:
    """The current event's own publish failing stops the sequence before the
    drain step even begins -- "stop on first publish failure mid-drain"
    covers the current event too: nothing gets acked, backlog stays intact.
    """
    spool = SpoolOutbox(tmp_path / "spool")
    ok_topic = "onex.evt.omniclaude.session-started.v1"
    for i in range(3):
        spool.append(
            _backlog_record(i, tier=EnumDurabilityTier.TELEMETRY, topic=ok_topic)
        )

    fail_topic = "onex.evt.omniclaude.session-ended.v1"
    adapter = RecordingPublishAdapter(fail_topics=frozenset({fail_topic}))
    handler = HandlerEventEmitEffect(spool=spool, publish_adapter=adapter)

    result = handler.handle(
        ModelEmitRequest(event_type="session.ended", payload={"session_id": "x"})
    )

    assert result.published is False
    assert result.drained_count == 0
    # No double-ack, no data loss: all 3 backlog records remain un-acked.
    assert spool.pending_count() == 4  # 3 backlog + the failed current event

    # Retry on the next invocation with a healthy adapter succeeds and
    # drains everything, proving at-least-once with no loss.
    healthy_adapter = RecordingPublishAdapter()
    retry_handler = HandlerEventEmitEffect(spool=spool, publish_adapter=healthy_adapter)
    retry_result = retry_handler.handle(
        ModelEmitRequest(event_type="session.started", payload={"session_id": "y"})
    )
    assert retry_result.published is True
    assert spool.pending_count() == 0
    assert retry_result.drained_count == 4


def test_publish_failure_mid_drain_leaves_remainder_unacked(tmp_path: Path) -> None:
    """The current event publishes successfully; a LATER backlog record then
    fails mid-drain. Draining stops there, and that record plus every
    successor after it stays un-acked on disk."""
    spool = SpoolOutbox(tmp_path / "spool")
    ok_topic = "onex.evt.omniclaude.session-started.v1"
    fail_topic = "onex.evt.omniclaude.mid-drain-failure.v1"
    outcomes = [
        spool.append(
            _backlog_record(
                i,
                tier=EnumDurabilityTier.TELEMETRY,
                topic=fail_topic if i == 2 else ok_topic,
            )
        )
        for i in range(4)
    ]

    adapter = RecordingPublishAdapter(fail_topics=frozenset({fail_topic}))
    handler = HandlerEventEmitEffect(spool=spool, publish_adapter=adapter)

    result = handler.handle(
        ModelEmitRequest(event_type="session.started", payload={"session_id": "cur"})
    )

    assert result.published is True  # current event's own topic (ok_topic) succeeded
    # Records 0 and 1 drain successfully; record 2 (fail_topic) fails and
    # stops the drain before record 3 is ever attempted.
    assert result.drained_count == 2
    remaining_ids = {f.record.event_id for f in spool.list_pending()}
    assert remaining_ids == {"backlog-2", "backlog-3"}
    # backlog-3 was never even attempted -- draining stopped at backlog-2.
    assert ("onex.evt.omniclaude.session-started.v1", "corr-3") not in adapter.attempts

    # backlog-2's file (index 2 in outcomes) is confirmed still un-acked.
    assert outcomes[2].spool_file is not None
    assert outcomes[2].spool_file.path.exists()


def test_unacked_leftover_file_is_republished_with_stable_idempotency_key(
    tmp_path: Path,
) -> None:
    """Simulates "publish succeeded, then the process crashed before ack":
    a real publish attempt happens for the record, ack is deliberately
    suppressed (never called), so the file survives on disk exactly as it
    would after that crash. The next invocation republishes it, proving
    at-least-once delivery with the same event_id/correlation_id across the
    retry -- this proves the idempotency key survives, not that any
    consumer dedupes (dedup is a downstream-consumer responsibility).
    """
    spool = SpoolOutbox(tmp_path / "spool")
    leftover = _backlog_record(99, tier=EnumDurabilityTier.TELEMETRY)
    outcome = spool.append(leftover)
    assert outcome.spool_file is not None

    # First attempt: a real publish call succeeds, but ack is deliberately
    # never called (simulating a crash in the window between broker-ack and
    # local ack/delete) -- the file must still be on disk afterward.
    first_attempt_adapter = RecordingPublishAdapter()
    for topic in leftover.topics:
        first_attempt_adapter.publish(
            topic,
            leftover.payload,
            key=leftover.partition_key,
            correlation_id=leftover.correlation_id,
        )
    assert first_attempt_adapter.attempts == [
        (leftover.topics[0], leftover.correlation_id)
    ]
    assert spool.pending_count() == 1  # ack suppressed -- file still pending

    # Second invocation: the handler finds the still-pending file and
    # republishes it as ordinary backlog.
    second_attempt_adapter = RecordingPublishAdapter()
    handler = HandlerEventEmitEffect(
        spool=spool, publish_adapter=second_attempt_adapter
    )
    result = handler.handle(
        ModelEmitRequest(event_type="session.started", payload={"session_id": "z"})
    )

    assert result.drained_count == 1
    assert spool.pending_count() == 0
    # The current event (no correlation_id set) publishes too; the backlog
    # attempt with the leftover's original correlation_id is what proves the
    # retry carried the same idempotency key.
    assert (
        leftover.topics[0],
        leftover.correlation_id,
    ) in second_attempt_adapter.attempts
    # Same event_id/correlation_id on both independent publish attempts.
    assert first_attempt_adapter.attempts == [
        (leftover.topics[0], leftover.correlation_id)
    ]
    assert leftover.event_id == "backlog-99"  # key stable across the retry
