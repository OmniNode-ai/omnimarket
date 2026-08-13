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
        timeout_seconds: float | None = None,
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


# ---------------------------------------------------------------------------
# OMN-15987 finding 3: event_id must not be able to land in a filesystem
# path outside the spool directory (spool_outbox.py embeds it verbatim in
# the filename).
# ---------------------------------------------------------------------------


def test_model_emit_request_rejects_event_id_with_path_separator() -> None:
    with pytest.raises(ValidationError):
        ModelEmitRequest(
            event_type="session.started",
            payload={},
            event_id="../../etc/passwd",
        )


def test_model_emit_request_rejects_event_id_with_forward_slash() -> None:
    with pytest.raises(ValidationError):
        ModelEmitRequest(event_type="session.started", payload={}, event_id="a/b")


def test_model_emit_request_accepts_filesystem_safe_event_id() -> None:
    request = ModelEmitRequest(
        event_type="session.started",
        payload={},
        event_id="valid-event.id_123",
    )
    assert request.event_id == "valid-event.id_123"


def test_model_emit_request_default_event_id_is_filesystem_safe() -> None:
    """The auto-generated event_id (str(uuid4())) must itself satisfy the
    new pattern constraint -- regression guard against the default and the
    validator drifting apart."""
    request = ModelEmitRequest(event_type="session.started", payload={})
    import re

    assert re.match(r"^[A-Za-z0-9._-]+$", request.event_id)


# ---------------------------------------------------------------------------
# OMN-15987 finding 4: topic override must work for an event_type outside
# the registry's scope entirely.
# ---------------------------------------------------------------------------


def test_topic_override_with_unregistered_event_type_succeeds(
    tmp_path: Path,
) -> None:
    adapter = FakePublishAdapter()
    spool = SpoolOutbox(tmp_path / "spool")
    handler = HandlerEventEmitEffect(spool=spool, publish_adapter=adapter)

    request = ModelEmitRequest(
        event_type="totally.unregistered.event",
        payload={"x": 1},
        topic="onex.evt.omnimarket.some-topic-not-in-the-registry.v1",
    )
    result = handler.handle(request)

    assert result.published is True
    assert result.topics_published == [
        "onex.evt.omnimarket.some-topic-not-in-the-registry.v1"
    ]
    assert len(adapter.calls) == 1


def test_topic_override_with_unregistered_event_type_defaults_to_telemetry_tier(
    tmp_path: Path,
) -> None:
    """Unregistered event_type + override topic must not silently inherit
    never-drop duty_critical semantics it was never declared for -- it
    defaults to the conservative (bounded, drop-oldest) telemetry tier."""
    spool = SpoolOutbox(
        tmp_path / "spool", max_telemetry_messages=1, max_duty_critical_messages=1
    )
    handler = HandlerEventEmitEffect(spool=spool, publish_adapter=None)

    handler.handle(
        ModelEmitRequest(
            event_type="unregistered.one",
            payload={},
            topic="onex.evt.omnimarket.unregistered-one.v1",
        )
    )
    # A second telemetry-tier event should evict the first (bounded,
    # drop-oldest) rather than raising SpoolFullError (which duty_critical
    # would).
    result = handler.handle(
        ModelEmitRequest(
            event_type="unregistered.two",
            payload={},
            topic="onex.evt.omnimarket.unregistered-two.v1",
        )
    )
    assert result.dropped_count == 1
    assert spool.pending_count() == 1


def test_topic_override_with_registered_event_type_keeps_registry_tier(
    tmp_path: Path,
) -> None:
    """When event_type IS registered, its registry-derived tier still
    applies even with an override topic -- the override changes where it
    publishes, not how durable it is."""
    spool = SpoolOutbox(tmp_path / "spool")
    handler = HandlerEventEmitEffect(spool=spool, publish_adapter=None)

    handler.handle(
        ModelEmitRequest(
            event_type="session.started",  # registered, telemetry-tier
            payload={},
            topic="onex.evt.omnimarket.session-started-override.v1",
        )
    )
    pending = spool.list_pending()
    assert len(pending) == 1
    from omnimarket.nodes.node_event_emit_effect.spool.topic_resolver import (
        EnumDurabilityTier,
    )

    assert pending[0].record.tier is EnumDurabilityTier.TELEMETRY


# ---------------------------------------------------------------------------
# OMN-15987 finding 1 (result shape): spool_only distinguishes "no adapter
# configured" from "publish attempted and failed".
# ---------------------------------------------------------------------------


def test_spool_only_true_when_no_adapter_configured(tmp_path: Path) -> None:
    spool = SpoolOutbox(tmp_path / "spool")
    handler = HandlerEventEmitEffect(spool=spool, publish_adapter=None)

    result = handler.handle(ModelEmitRequest(event_type="session.started", payload={}))
    assert result.published is False
    assert result.spool_only is True


def test_spool_only_false_when_publish_attempted_and_failed(
    tmp_path: Path,
) -> None:
    adapter = FakePublishAdapter(
        fail_topics=frozenset({"onex.evt.omniclaude.session-started.v1"})
    )
    spool = SpoolOutbox(tmp_path / "spool")
    handler = HandlerEventEmitEffect(spool=spool, publish_adapter=adapter)

    result = handler.handle(ModelEmitRequest(event_type="session.started", payload={}))
    assert result.published is False
    assert result.spool_only is False


def test_spool_only_false_when_publish_succeeds(tmp_path: Path) -> None:
    adapter = FakePublishAdapter()
    spool = SpoolOutbox(tmp_path / "spool")
    handler = HandlerEventEmitEffect(spool=spool, publish_adapter=adapter)

    result = handler.handle(ModelEmitRequest(event_type="session.started", payload={}))
    assert result.published is True
    assert result.spool_only is False


# ---------------------------------------------------------------------------
# OMN-15987 finding 2: shared publish budget across current-event publish +
# backlog drain, checked before every topic (not just between records).
# ---------------------------------------------------------------------------


class _SlowPublishAdapter:
    """Publishes instantly but records the timeout_seconds it was granted
    for every call -- used to assert the shared-deadline math without
    actually sleeping in the test."""

    def __init__(self) -> None:
        self.granted_timeouts: list[float | None] = []

    def publish(
        self,
        topic: str,
        payload: JsonType,
        *,
        key: str | None,
        correlation_id: str | None,
        timeout_seconds: float | None = None,
    ) -> None:
        self.granted_timeouts.append(timeout_seconds)


def test_drain_budget_is_shared_across_current_event_and_backlog(
    tmp_path: Path,
) -> None:
    """Every publish call (current event's own topics AND every backlog
    record's topics) must be granted a timeout_seconds derived from ONE
    shared deadline for the whole invocation -- not a separate fresh budget
    for the drain phase (the pre-fix overrun bug)."""
    spool = SpoolOutbox(tmp_path / "spool")
    adapter = _SlowPublishAdapter()
    backlog_handler = HandlerEventEmitEffect(spool=spool, publish_adapter=None)
    for i in range(3):
        backlog_handler.handle(
            ModelEmitRequest(event_type="session.started", payload={"i": i})
        )
    assert spool.pending_count() == 3

    handler = HandlerEventEmitEffect(spool=spool, publish_adapter=adapter)
    result = handler.handle(
        ModelEmitRequest(event_type="session.started", payload={"cur": True})
    )

    assert result.published is True
    assert result.drained_count == 3
    # 1 current-event publish + 3 backlog publishes, all sharing one budget.
    assert len(adapter.granted_timeouts) == 4
    for granted in adapter.granted_timeouts:
        assert granted is not None
        assert 0 < granted <= 5.0  # bounded by the shared deadline / single-publish cap


def test_drain_stops_when_shared_budget_is_exhausted(tmp_path: Path) -> None:
    """When the shared per-invocation budget is already exhausted before a
    backlog record's topic gets a turn, that record is left un-acked rather
    than published with a zero/negative timeout."""
    spool = SpoolOutbox(tmp_path / "spool")
    backlog_handler = HandlerEventEmitEffect(spool=spool, publish_adapter=None)
    for i in range(2):
        backlog_handler.handle(
            ModelEmitRequest(event_type="session.started", payload={"i": i})
        )
    assert spool.pending_count() == 2

    adapter = FakePublishAdapter()
    # A budget of 0 means the deadline is already in the past by the time
    # the current event's own publish is attempted.
    handler = HandlerEventEmitEffect(
        spool=spool, publish_adapter=adapter, drain_budget_seconds=0.0
    )
    result = handler.handle(
        ModelEmitRequest(event_type="session.started", payload={"cur": True})
    )

    assert result.published is False
    assert result.drained_count == 0
    assert spool.pending_count() == 3  # nothing acked: current + both backlog
