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


class HandlerSessionCompose:
    """Thin orchestrator composing session phases.

    In dry-run mode each phase is marked ``dry_run``. In live mode each phase
    is marked ``dispatched`` — real dispatch wiring is follow-up per the
    OMN-8812 plan.
    """

    def __init__(self, event_bus: ProtocolEventBus | Any | None = None) -> None:
        self._bus = event_bus

    def handle(self, command: ModelSessionComposeCommand) -> ModelSessionComposeResult:
        """Execute the session compose orchestration."""
        if command.dry_run:
            phase_results = [
                ModelPhaseResult(phase=phase, status="dry_run")
                for phase in command.phases
            ]
            return ModelSessionComposeResult(
                success=True,
                dry_run=True,
                phase_results=phase_results,
            )

        phase_results = [
            ModelPhaseResult(phase=phase, status="dispatched")
            for phase in command.phases
        ]
        return ModelSessionComposeResult(
            success=True,
            dry_run=False,
            phase_results=phase_results,
        )
