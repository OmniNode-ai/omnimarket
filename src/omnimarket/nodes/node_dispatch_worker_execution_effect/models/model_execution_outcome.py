# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Per-spec outcome model for dispatch-worker execution."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumDispatchWorkerExecutionStatus(StrEnum):
    DELEGATED = "delegated"
    DRY_RUN = "dry_run"
    FAILED = "failed"
    REJECTED = "rejected"
    SKIPPED_DUPLICATE = "skipped_duplicate"


class ModelDispatchWorkerExecutionOutcome(BaseModel):
    """Execution result for one dispatch-worker spec."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str
    ticket_id: str
    dispatch_id: str
    status: EnumDispatchWorkerExecutionStatus
    delegated: bool = False
    error: str = ""
    receipt_path: str = Field(default="")


__all__ = [
    "EnumDispatchWorkerExecutionStatus",
    "ModelDispatchWorkerExecutionOutcome",
]
