# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Emit daemon lifecycle handler.

Dispatches the lifecycle commands of the emit daemon (start, stop, status) onto
its lifecycle state machine. The handler holds no state of its own: the phase and
counters live in the ``EmitLifecycle`` collaborator it is given.

This handler orchestrates lifecycle only -- it does NOT process individual
events. The socket server and publisher loop run as async tasks managed
by the EmitSocketServer and KafkaPublisherLoop components.
"""

from __future__ import annotations

from omnimarket.handlers.runtime_paths import runtime_path
from omnimarket.nodes.node_emit_daemon.emit_lifecycle import (
    EmitLifecycle,
)
from omnimarket.nodes.node_emit_daemon.models.model_daemon_state import (
    ModelEmitDaemonCommand,
    ModelEmitDaemonCompletedEvent,
    ModelEmitDaemonState,
)


class HandlerEmitDaemon:
    """Lifecycle command handler for the emit daemon."""

    def __init__(self, lifecycle: EmitLifecycle | None = None) -> None:
        self._lifecycle = EmitLifecycle() if lifecycle is None else lifecycle

    @property
    def lifecycle(self) -> EmitLifecycle:
        return self._lifecycle

    def handle(
        self, request: ModelEmitDaemonCommand
    ) -> ModelEmitDaemonState | ModelEmitDaemonCompletedEvent:
        """Apply the lifecycle transition the request's action names.

        A start moves the lifecycle to listening, a stop completes it, and any
        other action (status, health) reports the state without moving it.
        """
        if request.action == "start":
            socket_path = request.socket_path or runtime_path("emit_daemon.socket_path")
            self._lifecycle.transition_to_binding(socket_path, request.pid)
            self._lifecycle.transition_to_listening()
            return self._lifecycle.state
        if request.action == "stop":
            return self._lifecycle.transition_to_stopped(
                events_published=request.events_published,
                events_dropped=request.events_dropped,
            )
        return self._lifecycle.state


__all__: list[str] = ["HandlerEmitDaemon"]
