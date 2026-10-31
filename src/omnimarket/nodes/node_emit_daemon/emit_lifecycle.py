# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Emit daemon lifecycle state machine (OMN-21536).

Manages the startup/shutdown lifecycle of the emit daemon:
    IDLE -> BINDING -> LISTENING -> DRAINING -> STOPPED

The phase and the counters are state, so they live here, in a collaborator the
lifecycle handler and the daemon's entry point are given, and not on the handler:
no public handler method assigns to the handler.

This machine orchestrates lifecycle only -- it does NOT process individual
events. The socket server and publisher loop run as async tasks managed
by the EmitSocketServer and KafkaPublisherLoop components.

Circuit breaker: after 3 consecutive startup failures, transitions to FAILED.
"""

from __future__ import annotations

import logging

from omnimarket.nodes.node_emit_daemon.models.model_daemon_state import (
    EnumEmitDaemonPhase,
    ModelEmitDaemonCompletedEvent,
    ModelEmitDaemonState,
)

logger = logging.getLogger(__name__)

CIRCUIT_BREAKER_THRESHOLD = 3

__all__: list[str] = ["CIRCUIT_BREAKER_THRESHOLD", "EmitLifecycle"]


class EmitLifecycle:
    """Lifecycle FSM state of the emit daemon.

    Responsible for state transitions only. Socket server and publisher
    loop are started/stopped by the caller around the transitions.
    """

    def __init__(self) -> None:
        self._state = ModelEmitDaemonState()

    @property
    def state(self) -> ModelEmitDaemonState:
        return self._state

    @property
    def phase(self) -> EnumEmitDaemonPhase:
        return self._state.phase

    def transition_to_binding(self, socket_path: str, pid: int) -> ModelEmitDaemonState:
        """Transition from IDLE to BINDING."""
        if self._state.phase != EnumEmitDaemonPhase.IDLE:
            raise ValueError(f"Cannot transition to BINDING from {self._state.phase}")
        self._state = ModelEmitDaemonState(
            phase=EnumEmitDaemonPhase.BINDING,
            socket_path=socket_path,
            pid=pid,
        )
        logger.info(f"Emit daemon transitioning to BINDING (socket={socket_path})")
        return self._state

    def transition_to_listening(self) -> ModelEmitDaemonState:
        """Transition from BINDING to LISTENING."""
        if self._state.phase != EnumEmitDaemonPhase.BINDING:
            raise ValueError(f"Cannot transition to LISTENING from {self._state.phase}")
        self._state = ModelEmitDaemonState(
            phase=EnumEmitDaemonPhase.LISTENING,
            socket_path=self._state.socket_path,
            pid=self._state.pid,
            started_at=self._state.started_at,
        )
        logger.info("Emit daemon now LISTENING")
        return self._state

    def transition_to_draining(self) -> ModelEmitDaemonState:
        """Transition from LISTENING to DRAINING."""
        if self._state.phase != EnumEmitDaemonPhase.LISTENING:
            raise ValueError(f"Cannot transition to DRAINING from {self._state.phase}")
        self._state = ModelEmitDaemonState(
            phase=EnumEmitDaemonPhase.DRAINING,
            socket_path=self._state.socket_path,
            pid=self._state.pid,
            events_queued=self._state.events_queued,
            events_published=self._state.events_published,
            events_dropped=self._state.events_dropped,
            started_at=self._state.started_at,
        )
        logger.info("Emit daemon DRAINING")
        return self._state

    def transition_to_stopped(
        self,
        events_published: int = 0,
        events_dropped: int = 0,
    ) -> ModelEmitDaemonCompletedEvent:
        """Transition to STOPPED from DRAINING or BINDING."""
        if self._state.phase not in (
            EnumEmitDaemonPhase.DRAINING,
            EnumEmitDaemonPhase.BINDING,
            EnumEmitDaemonPhase.LISTENING,
        ):
            raise ValueError(f"Cannot transition to STOPPED from {self._state.phase}")
        previous_phase = self._state.phase
        self._state = ModelEmitDaemonState(
            phase=EnumEmitDaemonPhase.STOPPED,
            events_published=events_published or self._state.events_published,
            events_dropped=events_dropped or self._state.events_dropped,
        )
        logger.info("Emit daemon STOPPED")
        return ModelEmitDaemonCompletedEvent(
            phase=EnumEmitDaemonPhase.STOPPED,
            previous_phase=previous_phase,
            events_published=self._state.events_published,
            events_dropped=self._state.events_dropped,
        )

    def transition_to_failed(self, error: str) -> ModelEmitDaemonCompletedEvent:
        """Transition to FAILED on unrecoverable error."""
        previous_phase = self._state.phase
        consecutive = self._state.consecutive_failures + 1
        self._state = ModelEmitDaemonState(
            phase=EnumEmitDaemonPhase.FAILED,
            error=error,
            consecutive_failures=consecutive,
            events_published=self._state.events_published,
            events_dropped=self._state.events_dropped,
        )
        logger.error(f"Emit daemon FAILED: {error} (consecutive: {consecutive})")
        return ModelEmitDaemonCompletedEvent(
            phase=EnumEmitDaemonPhase.FAILED,
            previous_phase=previous_phase,
            events_published=self._state.events_published,
            events_dropped=self._state.events_dropped,
            error=error,
        )

    def record_event_queued(self) -> None:
        """Increment queued counter."""
        if self._state.phase == EnumEmitDaemonPhase.LISTENING:
            self._state = self._state.model_copy(
                update={"events_queued": self._state.events_queued + 1}
            )

    def record_event_published(self) -> None:
        """Increment published counter."""
        self._state = self._state.model_copy(
            update={"events_published": self._state.events_published + 1}
        )

    def record_event_dropped(self) -> None:
        """Increment dropped counter."""
        self._state = self._state.model_copy(
            update={"events_dropped": self._state.events_dropped + 1}
        )

    def is_circuit_broken(self) -> bool:
        """Check if consecutive failures exceed threshold."""
        return self._state.consecutive_failures >= CIRCUIT_BREAKER_THRESHOLD

    def reset(self) -> None:
        """Reset to IDLE state for restart."""
        self._state = ModelEmitDaemonState()
        logger.info("Emit daemon lifecycle reset to IDLE")
