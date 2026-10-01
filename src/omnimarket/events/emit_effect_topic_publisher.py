# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Deliver one event to one contract topic through node_event_emit_effect (OMN-20154).

For a code path that has no orchestrator to return its event through (the
in-process ``onex delegate`` port, the judge), this is the one way it puts an
event on the bus: the emit effect spools it to disk first and then publishes
it, so an event made with no broker reachable is delivered on the effect's
next invocation instead of being lost.

The topic is always one the caller's own contract declares; this module adds
no topic of its own. Delivery never raises: the caller's primary work (a
delegation response) must not fail because telemetry could not be sent.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

logger = logging.getLogger(__name__)


class EmitEffectTopicPublisher:
    """Publish a JSON payload to a declared topic via ``node_event_emit_effect``."""

    def publish(
        self,
        *,
        topic: str,
        event_type: str,
        payload: Mapping[str, object],
        event_id: str,
        correlation_id: str | None = None,
        partition_key: str | None = None,
    ) -> bool:
        """Spool then publish. Returns whether the effect accepted the event."""
        try:
            from omnimarket.nodes.node_event_emit_effect import (
                HandlerEventEmitEffect,
                ModelEmitRequest,
            )

            HandlerEventEmitEffect().handle(
                ModelEmitRequest(
                    event_type=event_type,
                    topic=topic,
                    payload=dict(payload),
                    correlation_id=correlation_id,
                    partition_key=partition_key,
                    event_id=event_id,
                )
            )
        except Exception as exc:
            logger.warning(
                "event not delivered to %s (event_type=%s event_id=%s): %s",
                topic,
                event_type,
                event_id,
                exc,
            )
            return False
        return True


__all__ = ["EmitEffectTopicPublisher"]
