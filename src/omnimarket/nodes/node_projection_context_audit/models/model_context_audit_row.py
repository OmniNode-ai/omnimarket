# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One row of public.context_audit_events, with its deterministic key."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelContextAuditRow(BaseModel):
    """The twelve columns the retired writer inserted, plus event_key.

    violation_details is the canonical JSON text the writer binds to the
    table's JSONB column (sorted keys, no whitespace), so equal events give
    byte-equal parameters.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_key: str = Field(min_length=1)
    task_id: str
    parent_task_id: str | None
    correlation_id: str
    contract_id: str | None
    event_type: str
    enforcement_level: str
    enforcement_action: str | None
    violation_details: str | None
    context_tokens_used: int | None
    context_budget_tokens: int | None
    return_tokens: int | None
    return_max_tokens: int | None


__all__ = ["ModelContextAuditRow"]
