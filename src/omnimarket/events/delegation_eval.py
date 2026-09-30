# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared delegation-evaluation label event models."""

from __future__ import annotations

from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue


class ModelLabelRecordRequest(BaseModel):
    """One label of one delegation attempt, without a transport envelope."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: UUID
    correlation_id: str = Field(min_length=1)
    attempt_index: int = Field(ge=0)
    label: str = Field(min_length=1)
    rater_role: str = Field(min_length=1)
    rubric_version: str = Field(min_length=1)
    stratum: str
    computed_facts: dict[str, JsonValue]


class ModelDelegationEvalItemLabelled(ModelLabelRecordRequest):
    """Scrubbed label event persisted only in the lab table."""

    prompt_snapshot: str
    response_snapshot: str
    task_class: str
    gate_verdict: str
    deciding_check: str
    observed_at: AwareDatetime
