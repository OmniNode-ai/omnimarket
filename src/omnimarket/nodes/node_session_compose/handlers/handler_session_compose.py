# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Session compose handler — thin scaffold orchestrator.

Dry-run path returns a typed plan (status='dry_run' per phase). Live dispatch
currently returns a 'dispatched' placeholder; real skill-invocation wiring is
follow-up work tracked under the OMN-8812 epic.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ..models.model_phase_result import ModelPhaseResult
from ..models.model_session_compose_command import ModelSessionComposeCommand
from ..models.model_session_compose_result import ModelSessionComposeResult

if TYPE_CHECKING:
    from omnibase_core.protocols.event_bus.protocol_event_bus import ProtocolEventBus

__all__ = ["HandlerSessionCompose"]

logger = logging.getLogger(__name__)

SUCCESS_TOPIC = "onex.evt.omnimarket.session-compose-completed.v1"
FAILURE_TOPIC = "onex.evt.omnimarket.session-compose-failed.v1"


class HandlerSessionCompose:
    """Thin orchestrator composing session phases.

    In dry-run mode each phase is marked ``dry_run``. In live mode each phase
    is marked ``dispatched`` — real dispatch wiring is follow-up per the
    OMN-8812 plan.

    When an ``event_bus`` is supplied, the handler publishes the
    ``ModelSessionComposeResult`` to the contract-declared success/failure
    topic via ``publish_envelope``.
    """

    def __init__(self, event_bus: ProtocolEventBus | Any | None = None) -> None:
        self._bus = event_bus

    async def handle(
        self, command: ModelSessionComposeCommand
    ) -> ModelSessionComposeResult:
        """Execute the session compose orchestration."""
        if command.dry_run:
            phase_results = [
                ModelPhaseResult(phase=phase, status="dry_run")
                for phase in command.phases
            ]
            result = ModelSessionComposeResult(
                success=True,
                dry_run=True,
                phase_results=phase_results,
            )
        else:
            phase_results = [
                ModelPhaseResult(phase=phase, status="dispatched")
                for phase in command.phases
            ]
            result = ModelSessionComposeResult(
                success=True,
                dry_run=False,
                phase_results=phase_results,
            )

        await self._publish_result(result)
        return result

    async def _publish_result(self, result: ModelSessionComposeResult) -> None:
        """Publish the result to the contract-declared success/failure topic.

        Best-effort: logs and swallows publish failures so orchestration never
        fails solely due to bus unavailability.
        """
        if self._bus is None:
            return
        topic = SUCCESS_TOPIC if result.success else FAILURE_TOPIC
        publish_envelope = getattr(self._bus, "publish_envelope", None)
        if publish_envelope is None:
            logger.debug("event_bus has no publish_envelope; skipping publish")
            return
        try:
            await publish_envelope(envelope=result, topic=topic)
        except Exception:
            logger.exception("Failed to publish session compose result to %s", topic)
