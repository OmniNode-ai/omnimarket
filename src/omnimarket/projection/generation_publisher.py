# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Thin publisher for node-generation requests (OMN-13004).

A UI generate action posts a typed task description to the projection API's
``POST /api/generate`` route.  This module is the *only* logic behind that
route: it mints a correlation id, wraps the request in the canonical
``ModelEventEnvelope`` exactly the way the runtime / gate-zero publisher does
(``docs/evidence/2026-06-11-experiments/exp0/publish_exp0.py``), and publishes
ONE command to the canonical topic
``onex.cmd.omnimarket.node-generation-requested.v1``.

The existing ``node_generation_consumer`` does the generation, materialization,
invocation, registration, and emits the terminal
``onex.evt.omnimarket.node-generation-completed.v1`` event that the SEA Control
Plane projection renders.  Nothing in this module generates a node, talks to
Postgres, or invents state — it is a pure bus producer.

Design notes
------------
* The producer is created per-publish and closed immediately.  The projection
  API is GET-only and read-mostly; a generate submit is a rare, human-paced
  action, so a short-lived producer keeps the hot read path free of an
  always-on Kafka connection and matches the gate-zero publisher's lifecycle.
* Fail-fast: an empty task description raises before any bus connection; a
  broker that is unreachable surfaces as an exception the route maps to 503.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from aiokafka import AIOKafkaProducer
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.config.settings import Settings
from omnimarket.events.topics import NODE_GENERATION_REQUESTED_TOPIC_V1
from omnimarket.nodes.node_generation_consumer.models.model_generation import (
    ModelNodeGenerationRequest,
)

# Canonical command topic — verified live by gate-zero on 2026-06-11 (12/12
# terminal completions).  Sourced from the canonical topic registry
# (omnimarket.events.topics), the single owner of the literal; this local alias
# keeps the rest of the module readable.
NODE_GENERATION_REQUESTED_TOPIC = NODE_GENERATION_REQUESTED_TOPIC_V1

_SOURCE_TOOL = "omnidash-sea-control-plane"


class ModelGenerateRequest(BaseModel):
    """Typed body of ``POST /api/generate``.

    Only the task description is user-supplied.  ``max_attempts`` is bounded so
    a UI submit cannot request an unbounded repair loop.  Context-injection
    fields are intentionally omitted here — an interactive UI submit is the
    baseline (off-arm) path; the experiment harness owns context packs.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_description: str = Field(
        min_length=1,
        max_length=4000,
        description="Natural language description of the node to generate.",
    )
    max_attempts: int = Field(
        default=3,
        gt=0,
        le=10,
        description="Maximum LLM repair attempts on contract-validation failure.",
    )


class ModelGenerateResponse(BaseModel):
    """Response body of ``POST /api/generate`` — correlation id and nothing else.

    The UI polls the node-generation-completed projection for this
    correlation id to render the terminal artifact.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(
        description="Minted correlation id threaded through to the terminal event."
    )
    topic: str = Field(
        description="Canonical command topic the request was published to."
    )


def mint_correlation_id() -> str:
    """Mint a greppable, unique correlation id for a UI-originated generate.

    Shape mirrors the gate-zero ``exp0-...`` ids but tagged ``ui`` so live
    dashboard submits are distinguishable from experiment battery runs.
    """
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"ui-{stamp}-{uuid.uuid4().hex[:8]}"


def build_generation_envelope(
    request: ModelGenerateRequest, correlation_id: str
) -> ModelEventEnvelope[ModelNodeGenerationRequest]:
    """Wrap a generate request in the canonical envelope (exp0-identical shape).

    The structured correlation id rides on ``payload.correlation_id`` (str) —
    the value the generation consumer threads through to the terminal event and
    projection row — exactly as gate-zero's ``publish_exp0.py`` does.
    """
    payload = ModelNodeGenerationRequest(
        task_description=request.task_description,
        correlation_id=correlation_id,
        max_attempts=request.max_attempts,
        context_pack="",
        context_artifacts=[],
        context_pack_hash="",
    )
    return ModelEventEnvelope(
        payload=payload,
        envelope_timestamp=datetime.now(UTC),
        source_tool=_SOURCE_TOOL,
        event_type=NODE_GENERATION_REQUESTED_TOPIC,
    )


def _bootstrap_servers(settings: Settings | None = None) -> str:
    resolved = settings or Settings()
    bootstrap = resolved.get_effective_kafka_bootstrap_servers()
    if not bootstrap:
        raise RuntimeError(
            "KAFKA_BOOTSTRAP_SERVERS (or KAFKA_BROKER) is required to publish a "
            "node-generation request; the projection API has no broker configured."
        )
    return bootstrap


async def publish_generation_request(
    request: ModelGenerateRequest,
    *,
    settings: Settings | None = None,
    producer: AIOKafkaProducer | None = None,
) -> ModelGenerateResponse:
    """Publish ONE generation command to the canonical topic and return the cid.

    ``producer`` is injectable for tests; in production a short-lived producer
    is created and closed around the single ``send_and_wait``.  ``send_and_wait``
    means a returned response is proof the command is durably on the broker
    (not just buffered), so the UI's correlation id is honest.
    """
    correlation_id = mint_correlation_id()
    envelope = build_generation_envelope(request, correlation_id)
    value = envelope.model_dump_json().encode("utf-8")
    key = correlation_id.encode("utf-8")

    owns_producer = producer is None
    active = producer or AIOKafkaProducer(
        bootstrap_servers=_bootstrap_servers(settings), acks="all"
    )
    if owns_producer:
        await active.start()
    try:
        await active.send_and_wait(
            NODE_GENERATION_REQUESTED_TOPIC, value=value, key=key
        )
    finally:
        if owns_producer:
            await active.stop()

    return ModelGenerateResponse(
        correlation_id=correlation_id, topic=NODE_GENERATION_REQUESTED_TOPIC
    )
