# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Handler for the user-correction observer EFFECT node (OMN-12846).

Observes a typed ``ModelUserCorrectionEvent`` and republishes it as a durable
fact on the bus. The publish topic is resolved from injected config (which
mirrors ``event_bus.publish_topics`` in ``contract.yaml``) — this handler never
hardcodes a topic literal.

The republished fact carries the full category + failure-axis dimensions and the
mandatory context linkage. It is consumed downstream as a context-selection
signal ONLY (only the MISUNDERSTANDING axis counts against context selection);
it is never wired into an agent-output reward (anti-sycophancy invariant).
"""

from __future__ import annotations

import logging
from uuid import uuid4

from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput

from omnimarket.intelligence.events import ModelUserCorrectionEvent
from omnimarket.nodes.node_user_correction_observer_effect.models import (
    ModelUserCorrectionObserverConfig,
)

logger = logging.getLogger(__name__)

HANDLER_ID_USER_CORRECTION_OBSERVER = "user-correction-observer"


class HandlerUserCorrectionObserver:
    """EFFECT handler that republishes typed user corrections as durable facts.

    The publish topic is taken from the injected config's ``publish_topics``
    (first entry), which mirrors the contract's ``event_bus.publish_topics``.
    """

    def __init__(
        self,
        config: ModelUserCorrectionObserverConfig | None = None,
    ) -> None:
        self._config = (
            config if config is not None else ModelUserCorrectionObserverConfig()
        )

    @property
    def publish_topic(self) -> str:
        """Resolve the publish topic from config (contract-sourced)."""
        if not self._config.publish_topics:
            raise ValueError(
                "user-correction observer config declares no publish_topics; "
                "the topic must be sourced from contract.yaml event_bus"
            )
        return self._config.publish_topics[0]

    async def handle(
        self, request: ModelUserCorrectionEvent
    ) -> ModelHandlerOutput[None]:
        """Republish the observed correction as a durable EFFECT fact.

        The runtime adapter unwraps the inbound event and validates it into the
        typed ``ModelUserCorrectionEvent`` (the model validator rejects an orphan
        correction with no context linkage). The handler returns the typed fact;
        the runtime wraps it for the contract publish topic.
        """
        correction = request
        correlation_id = correction.correlation_id

        logger.info(
            "Republished user correction",
            extra={
                "handler": HANDLER_ID_USER_CORRECTION_OBSERVER,
                "category": correction.category.value,
                "failure_axis": correction.failure_axis.value,
                "counts_toward_context_failure": (
                    correction.counts_toward_context_failure
                ),
                "publish_topic": self.publish_topic,
            },
        )

        return ModelHandlerOutput.for_effect(
            input_envelope_id=uuid4(),
            correlation_id=correlation_id,
            handler_id=HANDLER_ID_USER_CORRECTION_OBSERVER,
            events=(correction,),
        )


__all__ = [
    "HANDLER_ID_USER_CORRECTION_OBSERVER",
    "HandlerUserCorrectionObserver",
]
