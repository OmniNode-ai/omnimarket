# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Use the platform runtime tick to request the contract's due observations."""

from __future__ import annotations

from omnibase_core.protocols.runtime.protocol_local_runtime_bus import (
    ProtocolLocalRuntimeBus,
)
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.models.model_runtime_tick_schedule import ModelRuntimeTickScheduleConfig
from omnimarket.nodes.contract_topics import contract_section
from omnimarket.nodes.node_runner_orchestrator.models.model_runner_version_watch import (
    ModelRunnerWatchRequest,
    ModelRunnerWatchResult,
)

from .handler_runner_version_watch import (
    CONTRACT_PATH,
    HandlerRunnerVersionWatch,
)


class HandlerRunnerVersionWatchTick:
    """No host timer or independent scheduler; the existing runtime drives this node."""

    def __init__(self, *, event_bus: ProtocolLocalRuntimeBus) -> None:
        self._watch = HandlerRunnerVersionWatch(event_bus=event_bus)
        self._schedule = ModelRuntimeTickScheduleConfig.model_validate(
            contract_section(CONTRACT_PATH, "runner_version_watch", "schedule")
        )

    async def handle(self, request: ModelRuntimeTick) -> ModelRunnerWatchResult | None:
        if (
            self._schedule.due_window(
                request.now, request.tick_interval_ms, scheduled_at=request.scheduled_at
            )
            is None
        ):
            return None
        return await self._watch.handle(
            ModelRunnerWatchRequest(
                correlation_id=request.correlation_id, now=request.now
            )
        )
