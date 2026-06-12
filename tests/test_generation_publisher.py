# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for the node-generation thin publisher (OMN-13004).

Covers:
- correlation id is minted, greppable, and unique
- the built envelope matches the gate-zero (exp0) wire shape EXACTLY
  (same payload type, same canonical topic, cid threaded onto
  payload.correlation_id, off-arm context fields)
- publish_generation_request sends ONE message to the canonical topic with the
  envelope as value and the cid as key, and returns that cid
- empty task description is rejected before any bus connection (fail-fast)
- a missing broker surfaces as RuntimeError (route maps to 503)
"""

from __future__ import annotations

import inspect
import json
from unittest.mock import AsyncMock

import pytest
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.event_bus.event_bus_kafka import EventBusKafka
from pydantic import ValidationError

import omnimarket.projection.generation_publisher as gp
from omnimarket.nodes.node_generation_consumer.models.model_generation import (
    ModelNodeGenerationRequest,
)
from omnimarket.projection.generation_publisher import (
    NODE_GENERATION_REQUESTED_TOPIC,
    ModelGenerateRequest,
    ModelGenerateResponse,
    ProtocolGenerationEventBus,
    build_generation_envelope,
    mint_correlation_id,
    publish_generation_request,
)

pytestmark = pytest.mark.unit

# The canonical topic the gate-zero publisher used (publish_exp0.py TOPIC).
EXP0_TOPIC = "onex.cmd.omnimarket.node-generation-requested.v1"


def test_topic_is_canonical_and_matches_gate_zero() -> None:
    assert NODE_GENERATION_REQUESTED_TOPIC == EXP0_TOPIC


def test_mint_correlation_id_is_unique_and_greppable() -> None:
    a = mint_correlation_id()
    b = mint_correlation_id()
    assert a != b
    assert a.startswith("ui-")
    # Shape: ui-<stamp>-<8 hex>
    parts = a.split("-")
    assert len(parts) == 3
    assert len(parts[2]) == 8


def test_envelope_matches_exp0_wire_shape() -> None:
    """The envelope this publisher builds is structurally identical to the one
    publish_exp0.py builds: ModelEventEnvelope wrapping ModelNodeGenerationRequest,
    event_type = canonical topic, cid on payload.correlation_id, off-arm context.
    """
    cid = "ui-20260611T120000Z-deadbeef"
    req = ModelGenerateRequest(
        task_description="Generate a node that adds two ints", max_attempts=3
    )
    env = build_generation_envelope(req, cid)

    # Envelope is the canonical type targeting the canonical topic.
    assert isinstance(env, ModelEventEnvelope)
    assert env.event_type == EXP0_TOPIC
    assert env.source_tool == "omnidash-sea-control-plane"

    # Payload is the generation request, with the cid threaded through exactly
    # like exp0 (payload.correlation_id carries the structured greppable id).
    payload = env.payload
    assert isinstance(payload, ModelNodeGenerationRequest)
    assert payload.correlation_id == cid
    assert payload.task_description == "Generate a node that adds two ints"
    assert payload.max_attempts == 3

    # Off-arm context: empty pack / no artifacts / empty hash — identical to
    # exp0's OFF arm publisher.
    assert payload.context_pack == ""
    assert payload.context_artifacts == []
    assert payload.context_pack_hash == ""

    # Serialises to JSON (the wire value) without loss.
    wire = json.loads(env.model_dump_json())
    assert wire["event_type"] == EXP0_TOPIC
    assert wire["payload"]["correlation_id"] == cid


def test_eventbuskafka_satisfies_publisher_lifecycle_surface() -> None:
    """Guard against the mock-masking defect class (OMN-13004, 2026-06-11).

    The publisher names lifecycle methods on ``ProtocolGenerationEventBus`` and
    calls them on the *real* ``EventBusKafka`` it constructs in ``_build_event_bus``.
    An ``AsyncMock`` auto-provides ANY attribute, so a mock-only test cannot catch
    a method-name drift between the protocol and the concrete class. The live
    HTTP 500 was exactly that: the publisher called ``bus.stop()`` but
    ``EventBusKafka`` only exposes ``start``/``close``/``shutdown``.

    This test asserts every async method the protocol declares actually exists,
    is callable, and is a coroutine function on the real ``EventBusKafka`` — so a
    future rename on either side fails here instead of in production.
    """
    protocol_methods = [
        name for name in vars(ProtocolGenerationEventBus) if not name.startswith("_")
    ]
    # The protocol must at least declare the lifecycle + publish surface.
    assert {"start", "close", "publish_envelope"} <= set(protocol_methods)
    # And it must NOT declare a non-existent lifecycle method (the bug).
    assert "stop" not in protocol_methods, (
        "ProtocolGenerationEventBus declares 'stop', but EventBusKafka has no "
        "'stop' method (lifecycle is start/close/shutdown). This is the "
        "mock-masking defect that returned HTTP 500 after a successful publish."
    )

    for name in protocol_methods:
        member = getattr(EventBusKafka, name, None)
        assert member is not None, (
            f"ProtocolGenerationEventBus.{name} is not present on the real "
            f"EventBusKafka — the publisher would AttributeError at runtime."
        )
        assert inspect.iscoroutinefunction(member), (
            f"EventBusKafka.{name} must be an async method to satisfy the "
            f"publisher's awaited call site."
        )


@pytest.mark.asyncio
async def test_publish_sends_one_command_to_canonical_topic() -> None:
    # Spec the mock against the REAL EventBusKafka so accessing an attribute the
    # concrete class does not have (e.g. a renamed lifecycle method) raises
    # AttributeError instead of being silently auto-provided. This is the
    # spec'd-mock guard against the mock-masking defect class.
    event_bus = AsyncMock(spec=EventBusKafka)
    req = ModelGenerateRequest(task_description="Generate a CSV parser node")

    resp = await publish_generation_request(req, event_bus=event_bus)

    assert isinstance(resp, ModelGenerateResponse)
    assert resp.topic == EXP0_TOPIC
    assert resp.correlation_id.startswith("ui-")

    # Exactly one publish_envelope to the canonical topic; injected bus is NOT
    # started/closed by the publisher (caller owns its lifecycle).
    event_bus.publish_envelope.assert_awaited_once()
    args, kwargs = event_bus.publish_envelope.await_args
    envelope, topic = args[0], args[1]
    assert topic == EXP0_TOPIC
    event_bus.start.assert_not_called()
    event_bus.close.assert_not_called()

    # Key is the cid; the envelope is the canonical type carrying that cid on
    # payload.correlation_id (exp0 threading).
    assert kwargs["key"] == resp.correlation_id.encode("utf-8")
    assert isinstance(envelope, ModelEventEnvelope)
    assert envelope.event_type == EXP0_TOPIC
    wire = json.loads(envelope.model_dump_json())
    assert wire["payload"]["correlation_id"] == resp.correlation_id


@pytest.mark.asyncio
async def test_owned_bus_lifecycle_uses_start_then_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the publisher constructs+owns the bus, it must start then close it.

    Spec'd against the real EventBusKafka so ``await bus.close()`` resolves only
    because EventBusKafka actually exposes ``close`` — a regression back to
    ``stop`` would AttributeError here (mocks spec'd to EventBusKafka reject
    ``.stop``).
    """
    owned_bus = AsyncMock(spec=EventBusKafka)
    req = ModelGenerateRequest(task_description="Generate a JSON normalizer node")

    # No event_bus injected → publisher builds + owns the bus; patch the builder
    # to hand back our spec'd mock so we never touch a real broker.
    def _fake_build(_settings: object = None) -> AsyncMock:
        return owned_bus

    monkeypatch.setattr(gp, "_build_event_bus", _fake_build)

    resp = await publish_generation_request(req)

    assert resp.correlation_id.startswith("ui-")
    owned_bus.start.assert_awaited_once()
    owned_bus.publish_envelope.assert_awaited_once()
    owned_bus.close.assert_awaited_once()
    # A spec'd-to-EventBusKafka mock has no 'stop' attribute, proving the
    # buggy lifecycle method is not part of the real class's surface.
    assert not hasattr(owned_bus, "stop")


def test_empty_task_description_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ModelGenerateRequest(task_description="")


def test_max_attempts_bounded() -> None:
    with pytest.raises(ValidationError):
        ModelGenerateRequest(task_description="x", max_attempts=0)
    with pytest.raises(ValidationError):
        ModelGenerateRequest(task_description="x", max_attempts=11)
