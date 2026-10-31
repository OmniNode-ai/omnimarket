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

from typing import Any

from omnimarket.handlers.runtime_paths import runtime_path
from omnimarket.nodes.node_emit_daemon.emit_lifecycle import (
    EmitLifecycle,
)


class HandlerEmitDaemon:
    """Lifecycle command handler for the emit daemon."""

    def __init__(self, lifecycle: EmitLifecycle | None = None) -> None:
        self._lifecycle = EmitLifecycle() if lifecycle is None else lifecycle

    @property
    def lifecycle(self) -> EmitLifecycle:
        return self._lifecycle

    def handle(self, input_data: dict[str, Any] | object) -> dict[str, Any]:
        """RuntimeLocal handler protocol shim.

        Delegates to the appropriate lifecycle transition based on the
        'action' field in input_data.
        """
        if hasattr(input_data, "model_dump"):
            input_data = input_data.model_dump(mode="json", exclude_none=True)
        if not isinstance(input_data, dict):
            raise TypeError("Emit daemon lifecycle command must be an object")
        action = input_data.get("action", "status")
        if action == "start":
            socket_path = input_data.get(
                "socket_path", runtime_path("emit_daemon.socket_path")
            )
            pid = input_data.get("pid", 0)
            self._lifecycle.transition_to_binding(socket_path, pid)
            self._lifecycle.transition_to_listening()
            return self._lifecycle.state.model_dump(mode="json")
        if action == "stop":
            completed = self._lifecycle.transition_to_stopped(
                events_published=input_data.get("events_published", 0),
                events_dropped=input_data.get("events_dropped", 0),
            )
            return completed.model_dump(mode="json")
        return self._lifecycle.state.model_dump(mode="json")


__all__: list[str] = ["HandlerEmitDaemon"]
