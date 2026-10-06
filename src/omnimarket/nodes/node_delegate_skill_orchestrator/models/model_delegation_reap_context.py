# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""First-claim context sufficient to answer an abandoned delegation."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from omnibase_core.models.delegation.wire import ModelDelegationProvenance
from pydantic import BaseModel, ConfigDict, field_validator

from omnimarket.nodes.node_delegate_skill_orchestrator.models.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)

# Shared by intake and recovery in this process; a new runtime gets a new id.
DELEGATION_RUNTIME_INSTANCE_ID = uuid4()


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
    runtime_instance_id: UUID | None = None
    request: ModelDelegateSkillRequest | None = None

    @field_validator("deadline_at")
    @classmethod
    def _utc_deadline(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("deadline_at must be timezone-aware")
        return value.astimezone(UTC)
