# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One unpublishable record must not hold the queue behind it (OMN-18627).

The drain stops at the first failure to preserve ordering. That is correct for
a transient failure: publishing the next record while this one is still owed
would reorder the stream. It is wrong for a record the broker will refuse every
time, because such a record is not owed -- it has left the stream.

Measured, 2026-09-17T20:43Z to 2026-09-18T16:29Z: eight records naming a topic
their principal had no WRITE grant on sat at the head of the emit spool and held
126 records of four AUTHORIZED classes behind them. Every journal publish paid a
full four-attempt authorization ladder, about nine seconds of wall clock, before
stopping on the same record again. The hook journal grew roughly 1,000 records
an hour against a 50,000 drop-oldest bound.

The poisoned record is written FIRST in every test here, so its filename sorts
ahead of the good ones under the spool's FIFO ordering. A test whose poison
sorted last would pass against the unfixed handler and prove nothing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from aiokafka.errors import TopicAuthorizationFailedError

from omnimarket.nodes.node_event_emit_effect.handlers.handler_event_emit_effect import (
    HandlerEventEmitEffect,
)
from omnimarket.nodes.node_event_emit_effect.models.model_emit_request import (
    JsonType,
    ModelEmitRequest,
)
from omnimarket.nodes.node_event_emit_effect.spool.enum_publish_outcome import (
    EnumPublishOutcome,
)
from omnimarket.nodes.node_event_emit_effect.spool.spool_outbox import SpoolOutbox

pytestmark = pytest.mark.unit

_UNGRANTED = "onex.evt.omniclaude.omn18627-ungranted.v1"


class _ClassifyingAdapter:
    """Refuses `_UNGRANTED` the way the transport does, accepts everything else.

    The refusal is raised WRAPPED, with the aiokafka error as `__cause__`,
    because that is the shape the fixed transport produces
    (`raise EventTopicAuthorizationError(...) from last_exception`). A test that
    raised the aiokafka error bare would pass against a classifier that only
    looked at the outermost exception and would therefore not cover the real
    path.
    """

    def __init__(self, *, transient_topics: frozenset[str] = frozenset()) -> None:
        self.published: list[str] = []
        self.attempts: list[str] = []
        self._transient = transient_topics

    def publish(
        self,
        topic: str,
        payload: JsonType,
        *,
        key: str | None,
        correlation_id: str | None,
        content_event_id: str | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        self.attempts.append(topic)
        if topic in self._transient:
            raise TimeoutError(f"simulated transient failure for {topic}")
        if topic == _UNGRANTED:
            cause = TopicAuthorizationFailedError(topic)
            raise RuntimeError(f"refused: {topic}") from cause
        self.published.append(topic)


def _spool_one(spool: SpoolOutbox, topic: str, event_id: str) -> Path:
    """Append one record directly, so ordering on disk is controlled exactly."""
    from datetime import UTC, datetime

    from omnimarket.nodes.node_event_emit_effect.spool.spool_outbox import SpoolRecord
    from omnimarket.nodes.node_event_emit_effect.spool.topic_resolver import (
        EnumDurabilityTier,
    )

    outcome = spool.append(
        SpoolRecord(
            event_id=event_id,
            event_type="team.evidence.written",
            topic=topic,
            tier=EnumDurabilityTier.TELEMETRY,
            payload={"session_id": "s", "task_id": event_id},
            partition_key="s",
            correlation_id=None,
            queued_at=datetime.now(UTC),
        )
    )
    assert outcome.spool_file is not None
    return outcome.spool_file.path


def test_poison_at_the_head_is_quarantined_and_the_rest_drain(tmp_path: Path) -> None:
    """The defect, exactly: one refused record ahead of two publishable ones."""
    spool = SpoolOutbox(tmp_path / "spool")
    poison = _spool_one(spool, _UNGRANTED, "poison-1")
    _spool_one(spool, "onex.evt.omniclaude.session-started.v1", "good-1")
    _spool_one(spool, "onex.evt.omniclaude.session-started.v1", "good-2")
    assert sorted(p.name for p in (tmp_path / "spool").glob("*.json"))[0] == poison.name

    adapter = _ClassifyingAdapter()
    handler = HandlerEventEmitEffect(spool=spool, publish_adapter=adapter)
    result = handler.handle(
        ModelEmitRequest(
            event_type="session.started", payload={"session_id": "current"}
        )
    )

    assert result.published is True
    assert result.drained_count == 2, (
        "both publishable backlog records must go out; the unfixed drain stops "
        f"at the poison and drains 0. Attempts: {adapter.attempts}"
    )
    assert result.quarantined_count == 1
    assert spool.pending_count() == 0
    assert spool.quarantined_count() == 1
    assert not poison.exists(), "the poisoned record must leave the pending queue"
    assert (spool.quarantine_dir / poison.name).is_file(), (
        "quarantine is a MOVE, never a delete -- the refusal is a statement "
        "about a missing grant, not about the record"
    )


def test_quarantined_record_keeps_its_bytes_and_carries_a_reason(
    tmp_path: Path,
) -> None:
    """The record is byte-identical, and the reason names the missing grant."""
    spool = SpoolOutbox(tmp_path / "spool")
    poison = _spool_one(spool, _UNGRANTED, "poison-2")
    original = poison.read_bytes()

    handler = HandlerEventEmitEffect(spool=spool, publish_adapter=_ClassifyingAdapter())
    handler.handle(
        ModelEmitRequest(
            event_type="session.started", payload={"session_id": "current"}
        )
    )

    moved = spool.quarantine_dir / poison.name
    assert moved.read_bytes() == original, (
        "a quarantined record must be replayable byte-for-byte once the grant "
        "exists; rewriting it would make the replay a different event"
    )

    reason = json.loads(
        (spool.quarantine_dir / f"{poison.stem}.reason.json").read_text()
    )
    assert reason["reason_code"] == "topic_authorization_denied"
    assert reason["topic"] == _UNGRANTED, (
        "the reason must NAME the topic whose grant is missing; a reader "
        "should not have to infer it from the payload"
    )
    assert reason["event_id"] == "poison-2"
    assert reason["schema_version"] == 1
    assert reason["quarantined_at"]
    assert reason["queued_at"]


def test_a_transient_failure_still_stops_the_drain(tmp_path: Path) -> None:
    """Positive control: stop-on-first-failure is preserved where it is right.

    Without this, the tests above are also satisfied by a drain that skips
    every failure and reorders the stream -- which would lose the ordering
    guarantee the stop exists to provide, silently.
    """
    spool = SpoolOutbox(tmp_path / "spool")
    slow = "onex.evt.omniclaude.tool-executed.v1"
    stuck = _spool_one(spool, slow, "transient-1")
    _spool_one(spool, "onex.evt.omniclaude.session-started.v1", "behind-1")

    adapter = _ClassifyingAdapter(transient_topics=frozenset({slow}))
    handler = HandlerEventEmitEffect(spool=spool, publish_adapter=adapter)
    result = handler.handle(
        ModelEmitRequest(
            event_type="session.started", payload={"session_id": "current"}
        )
    )

    assert result.drained_count == 0
    assert result.quarantined_count == 0
    assert stuck.exists(), "a retryable record stays on disk, un-acked"
    assert spool.pending_count() == 2
    assert spool.quarantined_count() == 0, (
        "a timeout is not a verdict the broker will repeat forever; "
        "quarantining it would discard a record that can still go out"
    )


def test_current_event_fanout_continues_past_an_ungranted_topic(
    tmp_path: Path,
) -> None:
    """One ungranted topic in a fan-out says nothing about the others.

    It also must not be left spooled: leaving it is precisely how it becomes
    the head of the queue on the next invocation.
    """
    spool = SpoolOutbox(tmp_path / "spool")
    handler = HandlerEventEmitEffect(spool=spool, publish_adapter=_ClassifyingAdapter())

    result = handler.handle(
        ModelEmitRequest(
            event_type="team.evidence.written",
            topic=_UNGRANTED,
            payload={"session_id": "s", "task_id": "t"},
        )
    )

    assert result.published is False
    assert result.topics_published == []
    assert result.quarantined_count == 1
    assert spool.pending_count() == 0, (
        "the refused record must not be left pending, or it is the next "
        "invocation's head-of-queue poison"
    )
    assert spool.quarantined_count() == 1


def test_classifier_reads_the_cause_chain_not_the_message(tmp_path: Path) -> None:
    """Classification is by TYPE, through the chain. Never by message text.

    A message match is a second definition of the same fact that diverges
    silently the first time a broker rewords a response, and the transport
    sanitizes its messages before they reach this node.
    """
    from omnimarket.nodes.node_event_emit_effect.handlers import (
        handler_event_emit_effect as module,
    )

    nested = RuntimeError("outer")
    nested.__cause__ = RuntimeError("middle")
    nested.__cause__.__cause__ = TopicAuthorizationFailedError("deep")
    assert module._is_topic_authorization_refusal(nested) is True

    # The words are present; the type is not.
    lookalike = RuntimeError(
        "TopicAuthorizationFailedError: not authorized for this client"
    )
    assert module._is_topic_authorization_refusal(lookalike) is False

    # A self-referential chain must terminate rather than spin inside the
    # drain's publish budget.
    loop_a = RuntimeError("a")
    loop_b = RuntimeError("b")
    loop_a.__cause__ = loop_b
    loop_b.__cause__ = loop_a
    assert module._is_topic_authorization_refusal(loop_a) is False


def test_outcome_enum_separates_owed_from_departed() -> None:
    """The three states are distinct; a bool could only carry two."""
    assert len({m.value for m in EnumPublishOutcome}) == 3
    assert EnumPublishOutcome.RETRYABLE is not EnumPublishOutcome.UNPUBLISHABLE
