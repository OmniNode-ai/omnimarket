# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Output model for swarm aggregator compute node."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelSwarmAggregateResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    aggregated_output: str
    aggregation_mode: str
    failed_subtasks: tuple[str, ...] = ()
    skipped_subtasks: tuple[str, ...] = ()
    degraded_reason: str = ""
    synthesis_input_hash: str = ""
    synthesis_model_id: str = ""
    run_id: str = ""
