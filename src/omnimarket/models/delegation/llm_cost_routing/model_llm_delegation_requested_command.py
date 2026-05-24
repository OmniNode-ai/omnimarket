# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Command model for initiating an LLM delegation request."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ModelLlmDelegationRequestedCommand(BaseModel):
    """Command emitted to initiate a delegation of an LLM task to a cheaper model.

    Raw prompt is not stored by default (privacy rule). Only the SHA-256 hash
    is included in this command for correlation and deduplication.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    correlation_id: str
    causation_id: str
    request_id: str
    task_type: str
    task_id: str | None
    model_id: str
    routing_policy_hash: str
    pricing_manifest_hash: str
    prompt_hash: str  # SHA-256 of prompt content; raw prompt NOT stored by default
    max_tokens: int
    temperature: float
    required_tier: str | None
    session_id: str | None
    repo_name: str | None
    created_at: datetime
