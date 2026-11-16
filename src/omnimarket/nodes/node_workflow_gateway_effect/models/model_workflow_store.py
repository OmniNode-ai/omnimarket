# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read-side rows and admission state of the gateway workflow store."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class ModelWorkflowRow(BaseModel):
    """Columns of ``public.gateway_workflows`` a read needs."""

    model_config = ConfigDict(frozen=True)

    workflow_id: UUID
    tenant_id: UUID
    workflow_type: str
    status: str
    envelope_id: UUID
    correlation_id: UUID
    command_topic: str
    submitted_at: datetime
    updated_at: datetime
    terminal_model_used: str | None = None
    terminal_total_tokens: int | None = None
    terminal_latency_ms: int | None = None
    terminal_route: str | None = None
    terminal_provider: str | None = None
    terminal_credential_source: str | None = None
    result_content: str | None = None
    terminal_failure_class: str | None = None
    terminal_failure_code: str | None = None
    terminal_failure_reason: str | None = None
    terminal_remediation: str | None = None
    terminal_rule_evaluations: Any = None


class ModelWorkflowQuotaStatus(BaseModel):
    """The tenant's monthly quota reading at admission time."""

    model_config = ConfigDict(frozen=True)

    status: Literal["ok", "soft_limit", "hard_limit_hit"]
    plan_code: str | None = None
    limit_value: int | None = None
    used: int | None = None
    remaining: int | None = None
    window_start: str | None = None
    window_end: str | None = None


class ModelWorkflowAdmission(BaseModel):
    """Whether the submission was admitted and the quota reading that decided it."""

    model_config = ConfigDict(frozen=True)

    admitted: bool
    quota: ModelWorkflowQuotaStatus


class ModelRateLimitDecision(BaseModel):
    """The outcome of one rate-limit check."""

    model_config = ConfigDict(frozen=True)

    allowed: bool
    remaining: int
    capacity: int
    refill_per_minute: int
    retry_after_seconds: int


__all__: list[str] = [
    "ModelRateLimitDecision",
    "ModelWorkflowAdmission",
    "ModelWorkflowQuotaStatus",
    "ModelWorkflowRow",
]
