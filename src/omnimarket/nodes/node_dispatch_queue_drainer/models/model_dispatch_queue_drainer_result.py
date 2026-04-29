# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Terminal result artifact model for dispatch queue drainer runs."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_dispatch_worker.models.model_dispatch_worker_command import (
    ModelDispatchWorkerCommand,
)
from omnimarket.nodes.node_dispatch_worker.models.model_dispatch_worker_result import (
    ModelDispatchWorkerResult,
)


class ModelDispatchQueueDrainerResult(BaseModel):
    """Terminal compile-only result for one queue-drainer run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["compiled", "blocked", "empty"]
    queue_item_path: str = ""
    result_artifact_path: str = ""
    blocked_reason: str = ""
    dispatch_worker_command: ModelDispatchWorkerCommand | None = None
    dispatch_worker_result: ModelDispatchWorkerResult | None = None
    processed_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))


__all__: list[str] = ["ModelDispatchQueueDrainerResult"]
