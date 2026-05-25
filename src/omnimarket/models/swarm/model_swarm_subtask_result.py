# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.models.swarm.enum_execution_status import EnumExecutionStatus


class ModelSwarmSubtaskResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    subtask_id: str
    worker_id: str
    execution_status: EnumExecutionStatus
    response_text: str = ""
    latency_ms: int = 0
    failure_reason: str = ""
    model_id: str = ""
