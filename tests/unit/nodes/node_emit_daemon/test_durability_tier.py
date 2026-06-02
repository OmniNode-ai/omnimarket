# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Tiered producer-edge durability tests for node_emit_daemon (OMN-12620).

Verifies the recorded decision (Option B, scoped by per-topic durability tier):

  * Tier is a declared per-topic property in the registry; loading a registry
    with a fan-out rule that has no tier fails fast.
  * Duty-critical events route to an append-only durable outbox with
    truncate-on-ack and NEVER drop; when the outbox is full the emit fails
    fast with explicit backpressure (no silent drop).
  * Telemetry events keep the bounded spool with drop-on-overflow.
  * A daemon restart loads pending outbox events (durable across restart).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from omnimarket.nodes.node_emit_daemon.durable_outbox import DurableOutbox
from omnimarket.nodes.node_emit_daemon.event_queue import (
    BoundedEventQueue,
    ModelQueuedEvent,
)
from omnimarket.nodes.node_emit_daemon.event_registry import EventRegistry
from omnimarket.nodes.node_emit_daemon.models.model_durability import (
    DurableOutboxFullError,
    EnumDurabilityTier,
)

pytestmark = pytest.mark.unit


def _event(
    event_id: str, tier: EnumDurabilityTier, payload_size: int = 0
) -> ModelQueuedEvent:
    return ModelQueuedEvent(
        event_id=event_id,
        event_type="delegation.request",
        topic="onex.cmd.omnibase-infra.delegation-request.v1",
        payload={"correlation_id": event_id, "pad": "x" * payload_size},
        partition_key=event_id,
        queued_at=datetime.now(UTC),
        tier=tier,
    )


# ---------------------------------------------------------------------------
# Registry: tier is required per fan-out rule
# ---------------------------------------------------------------------------


def test_registry_requires_tier_on_every_fan_out_rule(tmp_path: Path) -> None:
    """A fan-out rule with no tier fails fast at registry load."""
    registry_yaml = tmp_path / "topics.yaml"
    registry_yaml.write_text(
        "events:\n"
        "  delegation.request:\n"
        "    fan_out:\n"
        '      - topic: "onex.cmd.omnibase-infra.delegation-request.v1"\n'
        "        description: missing tier\n"
        "    required_fields: [correlation_id]\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="tier"):
        EventRegistry.from_yaml(registry_yaml)


def test_registry_parses_declared_tier(tmp_path: Path) -> None:
    registry_yaml = tmp_path / "topics.yaml"
    registry_yaml.write_text(
        "events:\n"
        "  delegation.request:\n"
        "    fan_out:\n"
        '      - topic: "onex.cmd.omnibase-infra.delegation-request.v1"\n'
        "        tier: duty_critical\n"
        "    required_fields: [correlation_id]\n"
        "  latency.breakdown:\n"
        "    fan_out:\n"
        '      - topic: "onex.evt.omniclaude.latency-breakdown.v1"\n'
        "        tier: telemetry\n"
        "    required_fields: [session_id]\n",
        encoding="utf-8",
    )
    registry = EventRegistry.from_yaml(registry_yaml)
    duty = registry.get_registration("delegation.request")
    telem = registry.get_registration("latency.breakdown")
    assert duty is not None
    assert telem is not None
    assert duty.fan_out[0].tier is EnumDurabilityTier.DUTY_CRITICAL
    assert telem.fan_out[0].tier is EnumDurabilityTier.TELEMETRY


def test_registry_rejects_unknown_tier(tmp_path: Path) -> None:
    registry_yaml = tmp_path / "topics.yaml"
    registry_yaml.write_text(
        "events:\n"
        "  delegation.request:\n"
        "    fan_out:\n"
        '      - topic: "onex.cmd.omnibase-infra.delegation-request.v1"\n'
        "        tier: bogus\n"
        "    required_fields: [correlation_id]\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="tier"):
        EventRegistry.from_yaml(registry_yaml)


def test_shipped_registry_declares_tier_for_every_fan_out_rule() -> None:
    """The shipped topics.yaml must declare a tier on every fan-out rule."""
    shipped = (
        Path(__file__).resolve().parents[4]
        / "src"
        / "omnimarket"
        / "nodes"
        / "node_emit_daemon"
        / "registries"
        / "topics.yaml"
    )
    # Loading must not raise: every fan-out rule carries a tier.
    registry = EventRegistry.from_yaml(shipped)
    assert len(registry) > 0
    for event_type in registry.list_event_types():
        reg = registry.get_registration(event_type)
        assert reg is not None
        for rule in reg.fan_out:
            assert isinstance(rule.tier, EnumDurabilityTier)


def test_shipped_registry_marks_duty_critical_topics() -> None:
    shipped = (
        Path(__file__).resolve().parents[4]
        / "src"
        / "omnimarket"
        / "nodes"
        / "node_emit_daemon"
        / "registries"
        / "topics.yaml"
    )
    registry = EventRegistry.from_yaml(shipped)
    duty_critical_topics: set[str] = set()
    telemetry_topics: set[str] = set()
    for event_type in registry.list_event_types():
        reg = registry.get_registration(event_type)
        assert reg is not None
        for rule in reg.fan_out:
            if rule.tier is EnumDurabilityTier.DUTY_CRITICAL:
                duty_critical_topics.add(rule.topic)
            else:
                telemetry_topics.add(rule.topic)

    # Duty-critical command / evidence topics named in the ticket.
    assert "onex.cmd.omnibase-infra.delegation-request.v1" in duty_critical_topics
    assert "onex.evt.omniclaude.dod-verify-completed.v1" in duty_critical_topics
    assert "onex.evt.omniclaude.audit-scope-violation.v1" in duty_critical_topics
    assert "onex.cmd.omniintelligence.session-outcome.v1" in duty_critical_topics
    assert "onex.evt.omniclaude.intent-commit-bound.v1" in duty_critical_topics

    # Telemetry topics named in the ticket.
    assert "onex.evt.omniclaude.latency-breakdown.v1" in telemetry_topics
    assert "onex.evt.omniclaude.phase-metrics.v1" in telemetry_topics
    assert "onex.evt.omniclaude.routing-feedback.v1" in telemetry_topics
    assert "onex.evt.omniclaude.circuit-breaker-tripped.v1" in telemetry_topics


# ---------------------------------------------------------------------------
# Durable outbox: append-only, truncate-on-ack, never drop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_outbox_append_and_load_after_restart(tmp_path: Path) -> None:
    outbox_dir = tmp_path / "outbox"
    outbox = DurableOutbox(outbox_dir=outbox_dir)
    await outbox.append(_event("e1", EnumDurabilityTier.DUTY_CRITICAL))
    await outbox.append(_event("e2", EnumDurabilityTier.DUTY_CRITICAL))
    assert outbox.pending_count() == 2

    # Simulate restart: fresh outbox over same dir loads pending events.
    restarted = DurableOutbox(outbox_dir=outbox_dir)
    loaded = await restarted.load_pending()
    assert loaded == 2
    assert restarted.pending_count() == 2


@pytest.mark.asyncio
async def test_outbox_truncate_on_ack(tmp_path: Path) -> None:
    outbox = DurableOutbox(outbox_dir=tmp_path / "outbox")
    await outbox.append(_event("e1", EnumDurabilityTier.DUTY_CRITICAL))
    record = await outbox.peek()
    assert record is not None
    assert record.event.event_id == "e1"
    # Not acked yet -> still pending and still on disk.
    assert outbox.pending_count() == 1
    await outbox.ack(record)
    assert outbox.pending_count() == 0
    # File is truncated (deleted) on ack.
    assert not record.path.exists()


@pytest.mark.asyncio
async def test_outbox_full_raises_never_drops(tmp_path: Path) -> None:
    """Outbox-full -> explicit backpressure error, never a silent drop."""
    outbox = DurableOutbox(outbox_dir=tmp_path / "outbox", max_messages=2)
    await outbox.append(_event("e1", EnumDurabilityTier.DUTY_CRITICAL))
    await outbox.append(_event("e2", EnumDurabilityTier.DUTY_CRITICAL))
    with pytest.raises(DurableOutboxFullError):
        await outbox.append(_event("e3", EnumDurabilityTier.DUTY_CRITICAL))
    # The two earlier duty-critical events are NOT dropped.
    assert outbox.pending_count() == 2


# ---------------------------------------------------------------------------
# Queue routing by tier
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_telemetry_drops_oldest_on_overflow(tmp_path: Path) -> None:
    queue = BoundedEventQueue(
        max_memory_queue=1,
        max_spool_messages=1,
        max_spool_bytes=10_485_760,
        spool_dir=tmp_path / "spool",
        outbox_dir=tmp_path / "outbox",
    )
    # memory(1) + spool(1) = capacity 2; third telemetry event drops oldest.
    assert await queue.enqueue(_event("t1", EnumDurabilityTier.TELEMETRY)) is True
    assert await queue.enqueue(_event("t2", EnumDurabilityTier.TELEMETRY)) is True
    assert await queue.enqueue(_event("t3", EnumDurabilityTier.TELEMETRY)) is True
    # Drop-on-overflow: total bounded at 2.
    assert queue.total_size() == 2
    assert queue.outbox_pending() == 0


@pytest.mark.asyncio
async def test_duty_critical_routes_to_outbox_and_never_drops(tmp_path: Path) -> None:
    queue = BoundedEventQueue(
        max_memory_queue=1,
        max_spool_messages=1,
        max_spool_bytes=10_485_760,
        spool_dir=tmp_path / "spool",
        outbox_dir=tmp_path / "outbox",
        max_outbox_messages=100,
    )
    for i in range(10):
        ok = await queue.enqueue(_event(f"d{i}", EnumDurabilityTier.DUTY_CRITICAL))
        assert ok is True
    # All 10 duty-critical survive even though spool capacity is 2.
    assert queue.outbox_pending() == 10
    # They are NOT in the lossy memory/spool path.
    assert queue.total_size() == 0


@pytest.mark.asyncio
async def test_duty_critical_outbox_full_raises(tmp_path: Path) -> None:
    queue = BoundedEventQueue(
        spool_dir=tmp_path / "spool",
        outbox_dir=tmp_path / "outbox",
        max_outbox_messages=1,
    )
    assert await queue.enqueue(_event("d1", EnumDurabilityTier.DUTY_CRITICAL)) is True
    with pytest.raises(DurableOutboxFullError):
        await queue.enqueue(_event("d2", EnumDurabilityTier.DUTY_CRITICAL))


@pytest.mark.asyncio
async def test_dequeue_prefers_then_drains_both_paths(tmp_path: Path) -> None:
    queue = BoundedEventQueue(
        spool_dir=tmp_path / "spool",
        outbox_dir=tmp_path / "outbox",
    )
    await queue.enqueue(_event("d1", EnumDurabilityTier.DUTY_CRITICAL))
    await queue.enqueue(_event("t1", EnumDurabilityTier.TELEMETRY))

    seen: set[str] = set()
    for _ in range(2):
        ev = await queue.dequeue()
        assert ev is not None
        seen.add(ev.event_id)
        await queue.ack(ev)
    assert seen == {"d1", "t1"}
    assert queue.outbox_pending() == 0
    assert queue.total_size() == 0


@pytest.mark.asyncio
async def test_queue_loads_outbox_on_restart(tmp_path: Path) -> None:
    spool = tmp_path / "spool"
    outbox = tmp_path / "outbox"
    q1 = BoundedEventQueue(spool_dir=spool, outbox_dir=outbox)
    await q1.enqueue(_event("d1", EnumDurabilityTier.DUTY_CRITICAL))
    await q1.enqueue(_event("d2", EnumDurabilityTier.DUTY_CRITICAL))

    # Restart: new queue over the same dirs loads pending outbox.
    q2 = BoundedEventQueue(spool_dir=spool, outbox_dir=outbox)
    loaded = await q2.load_outbox()
    assert loaded == 2
    assert q2.outbox_pending() == 2
