# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelSwarmConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    max_parallel_subtasks: int = 4
    max_subtasks_per_endpoint: int = 2
    per_endpoint_timeout_seconds: int = 120
    total_run_timeout_seconds: int = 600
    retry_policy_max_retries: int = 1
    fallback_policy_enabled: bool = True
