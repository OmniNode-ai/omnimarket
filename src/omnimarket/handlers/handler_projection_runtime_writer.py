# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Dispatch existing projection SQL and snapshots without a second consume loop."""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar

from omnimarket.projection.runner import (
    BaseProjectionRunner,
    MessageMeta,
    deterministic_correlation_id,
)


class RuntimeProjectionWriter:
    """One runtime delivery, with all asynchronous resources scoped to its loop.

    Composition keeps the Kafka ``run()`` entrypoint out of handler routing.
    Each delivery uses the existing writer's ``project_event`` implementation,
    including its snapshot and DLQ paths. The existing per-table topology
    resolver opens and attests each workload binding; this matters for savings,
    whose tenant writes and registry reads cannot share one login role.

    Profile names identify subscriptions, not processes. The composing runtime
    must activate the writer profiles and supply ONEX_DATABASE_TOPOLOGY_PROFILE
    and the topology's workload bindings, just as the dedicated writers did.
    """

    onex_runtime_inprocess_dispatch = True
    runner_type: ClassVar[type[BaseProjectionRunner]]

    def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        """Apply one delivery; never start a consumer or retain a pool."""
        data = dict(request)
        # The runtime's synchronous adapter is not an event payload or the
        # asynchronous writer's pool. Per-binding connections below are
        # resolved and attested by the same topology as that injected adapter.
        data.pop("_db", None)
        topic = str(data.pop("_topic"))
        partition = int(data.pop("_partition"))
        offset = int(data.pop("_offset"))
        meta = MessageMeta(
            topic=topic,
            partition=partition,
            offset=offset,
            fallback_id=str(
                data.pop(
                    "_fallback_id",
                    deterministic_correlation_id(topic, partition, offset),
                )
            ),
        )
        projected = asyncio.run(
            self.runner_type().project_runtime_event(
                topic, self.event_payload(data), meta
            )
        )
        return {"projected": projected}

    def event_payload(self, data: dict[str, Any]) -> dict[str, Any]:
        """Return the payload ``project_event`` receives, unchanged by default.

        A subclass narrows the payload here rather than overriding ``handle``,
        so every writer reads the source coordinates in one place.
        """
        return data
