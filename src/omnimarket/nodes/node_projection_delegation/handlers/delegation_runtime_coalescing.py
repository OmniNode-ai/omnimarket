# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A projection runner whose runtime deliveries commit before the aggregate views are read (OMN-23369).

The runtime delivery host keeps one runner and its event loop for the process
(``KeptPools``), and calls ``project_runtime_event`` once per message. That path
never goes through ``run()``, which is where the OMN-22874 coalescer was
enabled, so every delegation write re-read the four whole-table aggregate views
before its offset commit: 4 to 33 s per ``delegate-skill-completed`` message on
the dev lane. This base class enables the coalescer on the first delivery. The
republish then runs once per contract window, off the commit path, and is drained
when the runtime closes the writer.
"""

from __future__ import annotations

from typing import Any

from omnimarket.nodes.node_projection_delegation.handlers.delegation_aggregate_coalescer import (
    AggregateCoalescer,
)
from omnimarket.projection.runner import BaseProjectionRunner, MessageMeta


class CoalescingProjectionBase(BaseProjectionRunner):
    """Base runner that coalesces its aggregate republish in the consume loop and per delivery."""

    _coalescer: AggregateCoalescer
    # True once a runtime delivery enabled the coalescer: the runner then outlives
    # each delivery and `_stop_producer` leaves the producer and the pending
    # republish to the window and to `close_runtime_resources`.
    _kept_across_deliveries: bool = False

    async def run(self) -> None:
        if self._coalescer.window_s is not None:
            self._coalescer.enable()
        await super().run()

    async def project_runtime_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        """Apply one runtime delivery with the aggregate republish off its commit path."""
        if self._coalescer.window_s is not None and not self._coalescer.enabled:
            self._coalescer.enable()
        self._kept_across_deliveries = self._coalescer.enabled
        return await super().project_runtime_event(topic, data, meta)

    async def close_runtime_resources(self) -> None:
        # Publish what the window still holds while the pools are open, then let
        # the pools and the producer go.
        self._kept_across_deliveries = False
        await self._coalescer.drain()
        await super().close_runtime_resources()

    async def _stop_producer(self) -> None:
        # A delivery that returns on a kept runner leaves the producer and the
        # coalescer to the window: draining here would put the aggregate read
        # back on every commit, and stopping the producer would close it under
        # a flush in flight.
        if self._kept_across_deliveries:
            return
        # Every exit of run and shutdown stops the producer: publish what the
        # coalescer still holds first.
        await self._coalescer.drain()
        await super()._stop_producer()
