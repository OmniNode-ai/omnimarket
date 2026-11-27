# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The tenant outbox flush on the runtime's schedule.

handle takes the runtime tick. A tick inside the schedule overlay's interval returns nothing; the tick that is due builds the
flush handler from the flush node's own overlay (store and bus) and runs one pass with that overlay's batch limit, then
closes what it opened. The run event is completed when the pass ended flushed or with nothing to flush, and failed for every
other typed outcome or an overlay that cannot be read, naming it. A missing schedule overlay refuses once and returns nothing.
"""

from __future__ import annotations

import sys
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    NodeOverlayNotFoundError,
)
from omnimarket.handlers.schedule_gate import ScheduleGate
from omnimarket.handlers.scheduled_overlay_refusal import ScheduledOverlayRefusalLatch
from omnimarket.models.schedule_trigger.model_schedule_trigger import (
    ModelScheduleRunEvent,
)
from omnimarket.nodes.node_tenant_outbox_flush_effect.handlers.handler_tenant_outbox_flush import (
    publish_topic_from_contract,
)
from omnimarket.nodes.node_tenant_outbox_flush_effect.models.model_tenant_outbox_flush import (
    EnumTenantOutboxFlushOutcome,
    ModelTenantOutboxFlushCommand,
    ModelTenantOutboxFlushResult,
)
from omnimarket.nodes.node_tenant_outbox_flush_effect.overlay import (
    build_flush,
    load_overlay,
)
from omnimarket.nodes.node_tenant_outbox_flush_scheduled_effect.models import (
    ModelTenantOutboxFlushScheduleOverlay,
)

NODE_NAME = "node_tenant_outbox_flush_scheduled_effect"

_OK_OUTCOMES = frozenset(
    {
        EnumTenantOutboxFlushOutcome.FLUSHED,
        EnumTenantOutboxFlushOutcome.NOTHING_TO_FLUSH,
    }
)

# One flush pass: the typed answer. The default reads the flush node's overlay and connects; a test injects its own.
PassRunner = Callable[[], Awaitable[ModelTenantOutboxFlushResult]]


def _default_runner(environ: Mapping[str, str] | None) -> PassRunner:
    async def run() -> ModelTenantOutboxFlushResult:
        overlay = load_overlay(environ)
        handler, closers = build_flush(overlay, topic=publish_topic_from_contract())
        try:
            return await handler.handle(
                ModelTenantOutboxFlushCommand(
                    command_id=uuid4(), batch_limit=overlay.batch_limit
                )
            )
        finally:
            for close in closers:
                try:
                    await close()
                except Exception as exc:
                    sys.stderr.write(f"close failed: {type(exc).__name__}\n")

    return run


class HandlerTenantOutboxFlushScheduled:
    """Gate on the schedule, run one flush pass, return the run event."""

    def __init__(
        self,
        *,
        gate: ScheduleGate | None = None,
        runner: PassRunner | None = None,
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._gate = gate or ScheduleGate(
            NODE_NAME, model=ModelTenantOutboxFlushScheduleOverlay, environ=environ
        )
        self._runner = runner or _default_runner(environ)
        self._clock = clock
        self._refusals = ScheduledOverlayRefusalLatch(NODE_NAME)

    def _event(
        self,
        request: ModelRuntimeTick,
        outcome: Literal["completed", "failed"],
        detail: str,
    ) -> ModelScheduleRunEvent:
        return self._gate.run_event(
            tick_id=str(request.tick_id),
            scheduled_for=request.now,
            fired_at=self._clock(),
            outcome=outcome,
            detail=detail,
        )

    async def handle(self, request: ModelRuntimeTick) -> ModelScheduleRunEvent | None:
        try:
            overlay = self._gate.declaration()
            if not isinstance(overlay, ModelTenantOutboxFlushScheduleOverlay):
                raise NodeOverlayNotFoundError(
                    f"{NODE_NAME}: the overlay is not a tenant outbox flush schedule overlay"
                )
        except NodeOverlayNotFoundError as exc:
            self._refusals.refuse(exc)
            return None
        now = request.now.astimezone(UTC)
        if not self._gate.due(now):
            return None
        self._gate.mark(now)
        try:
            result = await self._runner()
        except NodeOverlayError:
            return self._event(
                request, "failed", "the tenant outbox flush overlay could not be read"
            )
        except Exception as exc:
            return self._event(
                request, "failed", f"the outbox flush pass raised {type(exc).__name__}"
            )
        return self._event(
            request,
            "completed" if result.outcome in _OK_OUTCOMES else "failed",
            f"{result.outcome.value}: {result.detail}",
        )


__all__: list[str] = ["HandlerTenantOutboxFlushScheduled"]
