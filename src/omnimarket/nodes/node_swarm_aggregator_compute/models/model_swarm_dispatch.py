# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelSwarmDispatch — local copy for aggregator node."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_swarm_aggregator_compute.models.enums import (
    EnumSubtaskStatus,
)


class ModelSwarmDispatchResult(BaseModel):
    """Inline result payload carried by a completed dispatch."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    response_text: str = ""
    handler_source: str = ""
    contract_yaml: str = ""
    task_description: str = ""


class ModelSwarmDispatch(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    subtask_id: str
    endpoint_id: str
    model_id: str
    base_url: str
    status: EnumSubtaskStatus
    result: ModelSwarmDispatchResult | None = None
    failure_class: str = ""
    failure_reason: str = ""
    retry_count: int = 0
    fallback_endpoint_id: str | None = None
    latency_ms: int = 0
    started_at: str = ""
    completed_at: str = ""
    wave: int = 0
