# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""node_emit_daemon -- portable event publishing daemon.

Accepts events via Unix domain socket, validates against a pluggable
per-platform event registry, queues with disk spool durability, and
publishes to Kafka.

Three runtime modes:
  - Standalone CLI: ``python -m omnimarket.nodes.node_emit_daemon``
  - Platform wrapper: started/stopped by IDE hooks (Claude Code, Cursor, etc.)
  - Kernel plugin: managed by omnibase_infra kernel on .201

The emit client is stdlib-only (socket + json) for maximum portability.
"""

from __future__ import annotations

__all__: list[str] = [
    "BoundedEventQueue",
    "EmitClient",
    "EmitSocketServer",
    "EventRegistry",
    "HandlerEmitDaemon",
    "KafkaPublisherLoop",
]


def __getattr__(name: str) -> object:
    if name == "EmitClient":
        from omnimarket.nodes.node_emit_daemon.client import EmitClient

        return EmitClient
    if name == "EmitSocketServer":
        from omnimarket.nodes.node_emit_daemon.socket_server import EmitSocketServer

        return EmitSocketServer
    if name == "BoundedEventQueue":
        from omnimarket.nodes.node_emit_daemon.event_queue import BoundedEventQueue

        return BoundedEventQueue
    if name == "EventRegistry":
        from omnimarket.nodes.node_emit_daemon.event_registry import EventRegistry

        return EventRegistry
    if name == "HandlerEmitDaemon":
        from omnimarket.nodes.node_emit_daemon.handlers import HandlerEmitDaemon

        return HandlerEmitDaemon
    if name == "KafkaPublisherLoop":
        from omnimarket.nodes.node_emit_daemon.publisher_loop import (
            KafkaPublisherLoop,
        )

        return KafkaPublisherLoop
    raise AttributeError(
        f"module 'omnimarket.nodes.node_emit_daemon' has no attribute {name!r}"
    )


from omnimarket.nodes.node_emit_daemon.handlers import HandlerEmitDaemon  # noqa: E402


class NodeEmitDaemon(HandlerEmitDaemon):
    """ONEX entry-point wrapper for HandlerEmitDaemon."""
