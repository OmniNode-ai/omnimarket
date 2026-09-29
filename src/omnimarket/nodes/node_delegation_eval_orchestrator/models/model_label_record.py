# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed label input, source snapshot, and scrubbed event payload."""

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


class ModelDelegationEventSnapshot(BaseModel):
    """Snapshot supplied by a tenant-bound delegation_events reader."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    prompt: str
    response: str
    task_class: str
    gate_verdict: str
    deciding_check: str


class ModelDelegationEvalItemLabelled(ModelLabelRecordRequest):
    """Scrubbed label event. Content is persisted only in the lab table.

    omnimarket is public: prompt/response content must never be checked into
    this repository or copied into the projection-applied notification.
    """

    prompt_snapshot: str
    response_snapshot: str
    task_class: str
    gate_verdict: str
    deciding_check: str
    observed_at: AwareDatetime


class ModelLabelRecordResult(BaseModel):
    """Topic and payload for the runtime-owned publishing effect."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    topic: str
    payload: ModelDelegationEvalItemLabelled
