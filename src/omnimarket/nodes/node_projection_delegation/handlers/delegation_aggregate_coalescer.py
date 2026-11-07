# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Coalesced republish of whole-table aggregate snapshots (OMN-22874).

A projection writer that republishes whole-table views after each write holds
every offset commit behind that read. This coalescer marks a tenant dirty per
write and publishes each dirty tenant once per contract-declared window, off the
commit path. The snapshot is latest-wins, so a reader loses at most one window
of freshness.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from omnimarket.models.model_message_meta import MessageMeta

logger = logging.getLogger(__name__)

PublishFn = Callable[..., Awaitable[None]]


def window_s_from_contract(contract: Mapping[str, Any]) -> float | None:
    """The ``aggregate_republish.coalesce_window_ms`` window in seconds.

    A contract with no ``aggregate_republish`` block gets ``None`` (inline
    republish). A declared window that is not a positive number is a contract
    error, refused rather than read as "no window".
    """
    block = contract.get("aggregate_republish")
    if block is None:
        return None
    window_ms = block.get("coalesce_window_ms") if isinstance(block, Mapping) else None
    if isinstance(window_ms, bool) or not isinstance(window_ms, int | float):
        raise ValueError(
            "aggregate_republish.coalesce_window_ms must be a number of "
            f"milliseconds, got {window_ms!r}"
        )
    if window_ms <= 0:
        raise ValueError(
            f"aggregate_republish.coalesce_window_ms must be positive, got {window_ms!r}"
        )
    return float(window_ms) / 1000.0


class AggregateCoalescer:
    """Marks tenants dirty and flushes them once per window."""

    def __init__(self, window_s: float | None, publish: PublishFn) -> None:
        self.window_s = window_s
        self.enabled = False
        self._publish = publish
        self._dirty: dict[str, MessageMeta] = {}
        self._task: asyncio.Task[None] | None = None
        self._flush_now = asyncio.Event()
        self._draining = False

    def enable(self) -> None:
        """Defer the republish; refused when the contract declares no window."""
        if self.window_s is None:
            raise RuntimeError(
                "aggregate coalescing needs aggregate_republish.coalesce_window_ms "
                "declared in the node contract"
            )
        self.enabled = True

    def mark_dirty(self, meta: MessageMeta, *, tenant: str) -> None:
        """Newest ``meta`` per tenant wins; schedule the one flush."""
        self._dirty[tenant] = meta
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._flush_loop())

    async def _flush_loop(self) -> None:
        while self._dirty:
            if not self._draining:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._flush_now.wait(), timeout=self.window_s
                    )
            await self._flush_once()
            if self._draining:
                return

    async def _flush_once(self) -> None:
        """One pass over the dirty tenants; a failed tenant stays dirty."""
        pending, self._dirty = self._dirty, {}
        for tenant, meta in pending.items():
            try:
                await self._publish(meta, tenant=tenant)
            except Exception as err:
                logger.error(
                    "OMN-22874: aggregate republish failed for tenant %s "
                    "(will retry): %s",
                    tenant,
                    err,
                )
                self._dirty.setdefault(tenant, meta)

    async def drain(self) -> None:
        """Publish whatever is pending now, one pass, without waiting."""
        self._draining = True
        self._flush_now.set()
        try:
            if self._task is not None and not self._task.done():
                await self._task
            elif self._dirty:
                await self._flush_once()
        finally:
            self._draining = False
            self._flush_now.clear()
