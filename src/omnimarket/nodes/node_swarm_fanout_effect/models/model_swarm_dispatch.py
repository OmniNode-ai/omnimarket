# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_swarm_fanout_effect.models.enums import EnumExecutionStatus


class ModelSwarmDispatch(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    subtask_id: str
    endpoint_id: str
    model_id: str
    base_url: str
    endpoint_status: str = ""
    execution_status: EnumExecutionStatus
    response_text: str = ""
    failure_class: str = ""
    failure_reason: str = ""
    retry_count: int = 0
    fallback_endpoint_id: str = ""
    started_at: str = ""
    completed_at: str = ""
    latency_ms: int = 0
    wave: int = 0
