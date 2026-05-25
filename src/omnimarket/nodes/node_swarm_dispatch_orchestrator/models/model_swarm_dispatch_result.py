# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Output model for swarm dispatch orchestrator."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_swarm_dispatch_orchestrator.models.enums import (
    EnumSwarmRunStatus,
)


class ModelSwarmDispatchResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    correlation_id: str
    status: EnumSwarmRunStatus
    aggregated_output: str
    subtask_count: int
    succeeded_count: int
    failed_count: int
    skipped_count: int
    total_latency_ms: int
    models_used: tuple[str, ...] = ()
    error: str = ""
