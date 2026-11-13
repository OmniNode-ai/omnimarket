# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request, row and result of the skill lifecycle projection fold."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class EnumSkillLifecycleEventType(StrEnum):
    """The two lifecycle events the skill_executions table records."""

    STARTED = "started"
    COMPLETED = "completed"


class ModelSkillLifecycleProjectionRequest(BaseModel):
    """One delivered skill lifecycle event: its topic and its raw payload."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    topic: str | None = Field(
        default=None, description="Source topic, None when the runtime named none."
    )
    payload: dict[str, object] = Field(
        description="The event payload with the delivery keys removed."
    )


class ModelSkillExecutionRow(BaseModel):
    """One skill_executions row, exactly the columns the consumer wrote."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: UUID = Field(description="Primary key and the conflict target.")
    run_id: UUID
    event_type: EnumSkillLifecycleEventType
    skill_name: str
    skill_id: str | None = None
    repo_id: str
    correlation_id: UUID
    args_count: int | None = None
    status: str | None = None
    duration_ms: int | None = None
    error_type: str | None = None
    started_emit_failed: bool = False
    session_id: str | None = None
    emitted_at: datetime


class ModelSkillLifecycleProjectionResult(BaseModel):
    """The row to write, or the reason the event writes nothing."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    row: ModelSkillExecutionRow | None = None
    skipped_reason: str | None = Field(
        default=None, description="Why no row was derived, None when one was."
    )
    key_derived: bool = Field(
        default=False,
        description="True when the producer sent no event id and the key was derived.",
    )
