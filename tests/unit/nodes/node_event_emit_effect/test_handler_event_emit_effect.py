# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for node_event_emit_effect's handler (OMN-15965 R1).

Covers:
- Happy path resolves topic(s)+tier from the registry and publishes.
- Multi-topic fan-out (e.g. prompt.submitted -> two topics).
- Unknown event_type fails fast (no silent default topic).
- Handler constructor does no I/O -- only handle() touches disk/network.
- Spool-only mode when KAFKA_BOOTSTRAP_SERVERS is unset.
- Models reject extra fields / empty event_type.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_event_emit_effect.handlers.handler_event_emit_effect import (
    HandlerEventEmitEffect,
)
from omnimarket.nodes.node_event_emit_effect.models.model_emit_request import (
    JsonType,
    ModelEmitRequest,
)
from omnimarket.nodes.node_event_emit_effect.spool.spool_outbox import SpoolOutbox
from omnimarket.nodes.node_event_emit_effect.spool.topic_resolver import (
    UnknownEventTypeError,
)

pytestmark = pytest.mark.unit


class FakePublishAdapter:
    """Records publish calls; never touches the network."""

    def __init__(self, *, fail_topics: frozenset[str] = frozenset()) -> None:
        self.calls: list[tuple[str, JsonType, str | None, str | None]] = []
        self._fail_topics = fail_topics

    def publish(
        self,
        topic: str,
        payload: JsonType,
        *,
        key: str | None,
        correlation_id: str | None,
    ) -> None:
        if topic in self._fail_topics:
            raise RuntimeError(f"simulated publish failure for {topic}")
        self.calls.append((topic, payload, key, correlation_id))


# ---------------------------------------------------------------------------
# Happy path / fan-out
# ---------------------------------------------------------------------------


def test_happy_path_resolves_topic_and_publishes(tmp_path: Path) -> None:
    adapter = FakePublishAdapter()
    spool = SpoolOutbox(tmp_path / "spool")
    handler = HandlerEventEmitEffect(spool=spool, publish_adapter=adapter)

    request = ModelEmitRequest(
        event_type="session.started", payload={"session_id": "abc"}
    )
    result = handler.handle(request)

    assert result.published is True
    assert result.topics_published == ["onex.evt.omniclaude.session-started.v1"]
    assert result.drained_count == 0
    assert result.dropped_count == 0
    assert result.event_id == request.event_id
    assert len(adapter.calls) == 1
    assert spool.pending_count() == 0  # acked after successful publish


def test_multi_topic_fan_out_publishes_all_topics(tmp_path: Path) -> None:
    adapter = FakePublishAdapter()
    spool = SpoolOutbox(tmp_path / "spool")
    handler = HandlerEventEmitEffect(spool=spool, publish_adapter=adapter)

    request = ModelEmitRequest(
        event_type="prompt.submitted",
        payload={"session_id": "s1", "prompt_preview": "hi"},
    )
    result = handler.handle(request)

    assert result.published is True
    assert set(result.topics_published) == {
        "onex.cmd.omniintelligence.claude-hook-event.v1",
        "onex.evt.omniclaude.prompt-submitted.v1",
    }
    assert len(adapter.calls) == 2


def test_explicit_topic_override(tmp_path: Path) -> None:
    adapter = FakePublishAdapter()
    spool = SpoolOutbox(tmp_path / "spool")
    handler = HandlerEventEmitEffect(spool=spool, publish_adapter=adapter)

    request = ModelEmitRequest(
        event_type="session.started",
        payload={"session_id": "abc"},
        topic="onex.evt.omnimarket.session-started-override.v1",
    )
    result = handler.handle(request)

    assert result.topics_published == [
        "onex.evt.omnimarket.session-started-override.v1"
    ]


# ---------------------------------------------------------------------------
# Unknown event_type
# ---------------------------------------------------------------------------


def test_unknown_event_type_fails_fast(tmp_path: Path) -> None:
    spool = SpoolOutbox(tmp_path / "spool")
    handler = HandlerEventEmitEffect(spool=spool, publish_adapter=FakePublishAdapter())
    request = ModelEmitRequest(event_type="totally.unregistered.event", payload={})

    with pytest.raises(UnknownEventTypeError):
        handler.handle(request)

    # No silent default topic: nothing should have been spooled either.
    assert spool.pending_count() == 0


# ---------------------------------------------------------------------------
# Purity: constructor does no I/O
# ---------------------------------------------------------------------------


def test_handler_constructor_performs_no_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spool_dir = tmp_path / "not-yet-created" / "spool"
    monkeypatch.setenv("ONEX_EMIT_EFFECT_SPOOL_DIR", str(spool_dir))
    monkeypatch.delenv("KAFKA_BOOTSTRAP_SERVERS", raising=False)

    HandlerEventEmitEffect()  # construction only -- no spool_dir override injected

    assert not spool_dir.exists(), (
        "handler __init__ must not touch disk; only handle() may create the spool dir"
    )


# ---------------------------------------------------------------------------
# Spool-only mode
# ---------------------------------------------------------------------------


def test_spool_only_mode_when_kafka_unconfigured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("KAFKA_BOOTSTRAP_SERVERS", raising=False)
    spool = SpoolOutbox(tmp_path / "spool")
    handler = HandlerEventEmitEffect(spool=spool)  # no publish_adapter injected

    request = ModelEmitRequest(
        event_type="session.started", payload={"session_id": "x"}
    )
    result = handler.handle(request)

    assert result.published is False
    assert result.topics_published == []
    assert result.drained_count == 0
    assert spool.pending_count() == 1  # accumulates until Kafka is configured


def test_oversized_current_event_is_dropped_but_backlog_still_drains(
    tmp_path: Path,
) -> None:
    """When the current event itself is too large to spool (oversized
    telemetry -- see SpoolOutbox._append_telemetry), it can't be published,
    but an existing backlog is still opportunistically drained."""
    spool = SpoolOutbox(tmp_path / "spool", max_telemetry_bytes=400)
    adapter = FakePublishAdapter()

    # Pre-populate a small backlog record that fits comfortably.
    backlog_handler = HandlerEventEmitEffect(
        spool=spool, publish_adapter=None
    )  # spool-only, so it just gets appended
    # Use the smallest possible request so it fits under the 400-byte cap.
    small_request = ModelEmitRequest(event_type="session.started", payload={})
    backlog_handler.handle(small_request)
    assert spool.pending_count() == 1

    handler = HandlerEventEmitEffect(spool=spool, publish_adapter=adapter)
    oversized_request = ModelEmitRequest(
        event_type="session.started",
        payload={"blob": "x" * 1000},
    )
    result = handler.handle(oversized_request)

    assert result.published is False
    assert result.topics_published == []
    assert result.dropped_count == 1  # the oversized current event itself
    assert result.drained_count == 1  # the pre-existing small backlog record
    assert spool.pending_count() == 0


# ---------------------------------------------------------------------------
# Model validation
# ---------------------------------------------------------------------------


def test_model_emit_request_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        ModelEmitRequest.model_validate(
            {"event_type": "session.started", "payload": {}, "bogus": "field"}
        )


def test_model_emit_request_rejects_empty_event_type() -> None:
    with pytest.raises(ValidationError):
        ModelEmitRequest(event_type="", payload={})


def test_model_emit_request_rejects_malformed_topic_override() -> None:
    with pytest.raises(ValidationError):
        ModelEmitRequest(
            event_type="session.started",
            payload={},
            topic="not-a-topic",
        )


def test_model_emit_request_accepts_well_formed_topic_override_outside_registry() -> (
    None
):
    """The override is a deliberate escape hatch -- shape-checked, not
    registry-membership-checked (see the field's own description)."""
    request = ModelEmitRequest(
        event_type="session.started",
        payload={},
        topic="onex.evt.omnimarket.some-topic-not-in-the-registry.v1",
    )
    assert request.topic == "onex.evt.omnimarket.some-topic-not-in-the-registry.v1"


def test_model_emit_result_rejects_extra_fields() -> None:
    from omnimarket.nodes.node_event_emit_effect.models.model_emit_result import (
        ModelEmitResult,
    )

    with pytest.raises(ValidationError):
        ModelEmitResult.model_validate(
            {"event_id": "e1", "published": True, "bogus": "field"}
        )
