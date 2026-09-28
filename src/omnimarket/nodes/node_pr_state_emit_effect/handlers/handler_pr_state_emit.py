# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Type a watcher observation, then delegate delivery to node_event_emit_effect."""

from typing import Protocol

from omnimarket.nodes.node_event_emit_effect.handlers.handler_event_emit_effect import (
    HandlerEventEmitEffect,
)
from omnimarket.nodes.node_event_emit_effect.models.model_emit_request import (
    ModelEmitRequest,
)
from omnimarket.nodes.node_event_emit_effect.models.model_emit_result import (
    ModelEmitResult,
)
from omnimarket.nodes.node_pr_state_emit_effect.contract_topics import (
    EVENT_TYPE,
    TOPIC_PR_STATE_OBSERVED,
)
from omnimarket.nodes.node_pr_state_emit_effect.models.model_pr_state_emit_request import (
    ModelPrStateEmitRequest,
)
from omnimarket.nodes.node_pr_state_emit_effect.models.model_pr_state_emit_result import (
    ModelPrStateEmitResult,
)
from omnimarket.nodes.node_pr_state_emit_effect.models.model_pr_state_observed_event import (
    ModelPrStateObservedEvent,
)


class ProtocolEventEmitter(Protocol):
    def handle(self, request: ModelEmitRequest) -> ModelEmitResult: ...


class HandlerPrStateEmit:
    def __init__(self, *, emitter: ProtocolEventEmitter | None = None) -> None:
        self._emitter = emitter

    def handle(self, request: ModelPrStateEmitRequest) -> ModelPrStateEmitResult:
        event = ModelPrStateObservedEvent.model_validate(request.model_dump())
        try:
            emitted = self.emit_event(event)
        except Exception as exc:  # The effect boundary returns spool refusals as data.
            return ModelPrStateEmitResult(
                accepted=False,
                digest=event.digest,
                event_type=EVENT_TYPE,
                topic=TOPIC_PR_STATE_OBSERVED,
                error=repr(exc),
            )
        return ModelPrStateEmitResult(
            accepted=True,
            published=emitted.published,
            digest=event.digest,
            event_type=EVENT_TYPE,
            topic=TOPIC_PR_STATE_OBSERVED,
        )

    def emit_event(self, event: ModelPrStateObservedEvent) -> ModelEmitResult:
        emitter = (
            self._emitter if self._emitter is not None else HandlerEventEmitEffect()
        )
        return emitter.handle(
            ModelEmitRequest(
                event_type=EVENT_TYPE,
                payload=event.model_dump(mode="json"),
                event_id=event.digest,
                correlation_id=event.digest,
                partition_key=f"{event.repo}#{event.pr_number}",
            )
        )
