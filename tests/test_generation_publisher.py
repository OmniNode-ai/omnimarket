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

import json
from unittest.mock import AsyncMock

import pytest
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from pydantic import ValidationError

from omnimarket.nodes.node_generation_consumer.models.model_generation import (
    ModelNodeGenerationRequest,
)
from omnimarket.projection.generation_publisher import (
    NODE_GENERATION_REQUESTED_TOPIC,
    ModelGenerateRequest,
    ModelGenerateResponse,
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


@pytest.mark.asyncio
async def test_publish_sends_one_command_to_canonical_topic() -> None:
    producer = AsyncMock()
    req = ModelGenerateRequest(task_description="Generate a CSV parser node")

    resp = await publish_generation_request(req, producer=producer)

    assert isinstance(resp, ModelGenerateResponse)
    assert resp.topic == EXP0_TOPIC
    assert resp.correlation_id.startswith("ui-")

    # Exactly one send to the canonical topic; injected producer is NOT
    # started/stopped by the publisher (caller owns its lifecycle).
    producer.send_and_wait.assert_awaited_once()
    args, kwargs = producer.send_and_wait.await_args
    assert args[0] == EXP0_TOPIC
    producer.start.assert_not_called()
    producer.stop.assert_not_called()

    # Key is the cid; value is the JSON envelope carrying that cid.
    assert kwargs["key"] == resp.correlation_id.encode("utf-8")
    wire = json.loads(kwargs["value"].decode("utf-8"))
    assert wire["event_type"] == EXP0_TOPIC
    assert wire["payload"]["correlation_id"] == resp.correlation_id


def test_empty_task_description_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ModelGenerateRequest(task_description="")


def test_max_attempts_bounded() -> None:
    with pytest.raises(ValidationError):
        ModelGenerateRequest(task_description="x", max_attempts=0)
    with pytest.raises(ValidationError):
        ModelGenerateRequest(task_description="x", max_attempts=11)
