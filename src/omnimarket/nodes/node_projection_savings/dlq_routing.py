# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Routes a malformed savings event to the DLQ and remembers it did (OMN-21536).

The apply seam returns True for a quarantined event so the consumer commits its
offset, which makes a malformed event indistinguishable from a projected one at
the ``project_event`` seam (OMN-17426). This collaborator routes the event and
keeps the mark that tells them apart. The mark is state, so it lives here, in a
collaborator the runner is given, and not on the runner: a public runner method
never assigns to the runner.
"""

from __future__ import annotations

from typing import Any

from omnimarket.projection.dlq import PublishFn, route_to_dlq

__all__: list[str] = ["DlqRouting"]


class DlqRouting:
    """The DLQ outcome of the event currently being applied."""

    def __init__(self) -> None:
        self._quarantined = False

    @property
    def quarantined(self) -> bool:
        """True when the event being applied was routed to the DLQ."""
        return self._quarantined

    def begin_event(self) -> None:
        """Forget the previous event's outcome, at the top of every apply."""
        self._quarantined = False

    async def route(
        self,
        *,
        publish: PublishFn | None,
        dlq_topics: list[str],
        original_message: dict[str, Any],
        failure_reason: str,
        handler: str,
        correlation_id: str,
    ) -> None:
        """Mark the event quarantined, then publish it to the DLQ topic."""
        self._quarantined = True
        await route_to_dlq(
            publish=publish,
            dlq_topics=dlq_topics,
            original_message=original_message,
            failure_reason=failure_reason,
            handler=handler,
            correlation_id=correlation_id,
        )
