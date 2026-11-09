# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The join gate on a topic with no producer: a typed upstream failure is refused and the consumer does not start (OMN-23151).

A message that arrives on the topic is either an event, which starts the consumer through the injected start port, or the
producer side's typed failure, which does not. A payload that is neither valid JSON nor a valid typed failure, or a
failure that names another topic, is refused too: the consumer never starts on a message it cannot tell from an event
by its shape alone. A refusal carries the code the consumer would have answered with: ``upstream_<failure_code>``.
"""

from __future__ import annotations

import json

from pydantic import ValidationError

from omnimarket.nodes.node_seam_reading_compute.adapters.inmemory_consumer_start import (
    InMemoryConsumerStart,
)
from omnimarket.nodes.node_seam_reading_compute.models.model_no_producer_topic import (
    EnumJoinOutcome,
    ModelNoProducerJoinRequest,
    ModelNoProducerJoinResult,
    ModelUpstreamTypedFailure,
)
from omnimarket.nodes.node_seam_reading_compute.protocols import ProtocolConsumerStart

UPSTREAM_PREFIX = "upstream_"
_FAILURE_KIND = "typed_failure"


class HandlerNoProducerJoin:
    """Start the consumer on an event of its topic; refuse a typed failure without starting it."""

    def __init__(self, *, start: ProtocolConsumerStart | None = None) -> None:
        self._start: ProtocolConsumerStart = (
            start if start is not None else InMemoryConsumerStart()
        )

    def handle(self, request: ModelNoProducerJoinRequest) -> ModelNoProducerJoinResult:
        def refused(code: str, detail: str) -> ModelNoProducerJoinResult:
            return ModelNoProducerJoinResult(
                topic=request.topic,
                consumer=request.consumer,
                outcome=EnumJoinOutcome.REFUSED,
                consumer_started=False,
                failure_code=code,
                detail=detail,
                decided_at=request.now,
            )

        try:
            body = json.loads(request.payload)
        except json.JSONDecodeError as exc:
            return refused("bad-payload", f"not JSON: {exc.msg}")
        if isinstance(body, dict) and body.get("kind") == _FAILURE_KIND:
            try:
                failure = ModelUpstreamTypedFailure.model_validate(body)
            except ValidationError as exc:
                fields = sorted(
                    {
                        ".".join(str(p) for p in e["loc"]) or "<document>"
                        for e in exc.errors()
                    }
                )
                return refused("bad-payload", f"typed failure invalid: {fields}")
            if failure.topic != request.topic:
                return refused(
                    "topic-mismatch",
                    f"failure names {failure.topic}, message arrived on {request.topic}",
                )
            return refused(
                f"{UPSTREAM_PREFIX}{failure.failure_code}",
                f"{request.consumer} does not start: {failure.detail}",
            )
        self._start.start(request.consumer, request.topic, request.payload)
        return ModelNoProducerJoinResult(
            topic=request.topic,
            consumer=request.consumer,
            outcome=EnumJoinOutcome.STARTED,
            consumer_started=True,
            decided_at=request.now,
        )
