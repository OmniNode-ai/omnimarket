# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Wire shape of one context-integrity audit event."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, JsonValue


class ModelContextAuditEventWire(BaseModel):
    """One audit event as omniclaude publishes it on the five audit topics.

    The four required fields are the ones the retired consumer's schema
    pre-filter required (it skipped a message missing any of them). Here a
    message missing one fails validation and is dead-lettered, so it is never
    silently dropped. Unknown keys are ignored, as the retired module ignored
    them.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    task_id: str
    correlation_id: str
    event_type: str
    enforcement_level: str
    parent_task_id: str | None = None
    contract_id: str | None = None
    enforcement_action: str | None = None
    violation_details: JsonValue | None = None
    context_tokens_used: int | None = None
    context_budget_tokens: int | None = None
    return_tokens: int | None = None
    return_max_tokens: int | None = None


__all__ = ["ModelContextAuditEventWire"]
