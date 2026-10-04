# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""First-claim context sufficient to answer an abandoned delegation."""

from datetime import UTC, datetime
from uuid import UUID

from omnibase_core.models.delegation.wire import ModelDelegationProvenance
from pydantic import BaseModel, ConfigDict, field_validator


class ModelDelegationReapContext(BaseModel):
    """Immutable deadline and terminal attribution for one command."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    task_type: str
    tenant_id: str | None
    ticket_id: str | None
    caller_lane: str | None
    session_id: str | None
    provenance: ModelDelegationProvenance | None
    deadline_at: datetime

    @field_validator("deadline_at")
    @classmethod
    def _utc_deadline(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("deadline_at must be timezone-aware")
        return value.astimezone(UTC)
