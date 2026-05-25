# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelSwarmSubtaskAssignment(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    subtask_id: str
    worker_id: str
    description: str
    model_id: str
    endpoint_url: str
    timeout_seconds: int = 120
    correlation_id: str
