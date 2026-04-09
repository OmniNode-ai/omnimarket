# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
import logging

from ..models.model_pipeline_completed_event import PipelineCompletedEvent
from ..models.model_pipeline_phase_event import PipelinePhaseEvent
from ..models.model_pipeline_start_command import PipelineStartCommand
from ..models.model_pipeline_state import PipelineState

logger = logging.getLogger(__name__)


class HandlerPrReviewBot:
    """
    Finite State Machine handler for Pull Request Review Bot operations.
    Manages state transitions based on pipeline events and commands.
    """

    def __init__(self) -> None:
        self._state: PipelineState = PipelineState.INITIALIZED

    @property
    def state(self) -> PipelineState:
        """Current state of the PR Review Bot."""
        return self._state

    def start(self, command: PipelineStartCommand) -> None:
        """Transition from INITIALIZED to RUNNING state."""
        if self._state != PipelineState.INITIALIZED:
            raise RuntimeError("Cannot start from current state")

        self._state = PipelineState.RUNNING
        # Implementation: Start PR review process
        logger.info(f"Starting PR review for ticket: {command.ticket_id}")

    def handle_phase_event(self, event: PipelinePhaseEvent) -> None:
        """Process phase events during RUNNING state."""
        if self._state != PipelineState.RUNNING:
            raise RuntimeError("Cannot handle phase event in current state")

        # Implementation: Handle specific review phases
        logger.info(f"Processing phase: {event.phase_name}")

    def complete(self, event: PipelineCompletedEvent) -> None:
        """Transition from RUNNING to COMPLETED state."""
        if self._state != PipelineState.RUNNING:
            raise RuntimeError("Cannot complete from current state")

        self._state = PipelineState.COMPLETED
        # Implementation: Finalize PR review
        logger.info(f"Completed PR review with result: {event.result}")

    def reset(self) -> None:
        """Reset handler to initial state."""
        self._state = PipelineState.INITIALIZED
        logger.info("Reset PR Review Bot to initial state")
