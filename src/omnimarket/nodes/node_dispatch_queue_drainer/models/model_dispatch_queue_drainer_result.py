# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Terminal result artifact model for dispatch queue drainer runs."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelDispatchQueueDrainerResult(BaseModel):
    """Terminal compile-only result for one queue-drainer run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["compiled", "blocked", "empty"]
    queue_item_path: str = ""
    result_artifact_path: str = ""
    blocked_reason: str = ""
    dispatch_worker_command: dict[str, object] | None = None
    dispatch_worker_result: dict[str, object] | None = None
    processed_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))


__all__: list[str] = ["ModelDispatchQueueDrainerResult"]
