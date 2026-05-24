# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Request model for the LLM cost-routing delegation framework.

Distinct from model_delegation_request.py in the delegation/ root, which is
for task dispatch. This model is for the cost-tracking routing framework (OMN-11771).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelLlmDelegationRequest(BaseModel):
    """Input to the LLM delegation routing pipeline.

    The prompt field is in-memory only and must never be persisted by default
    (privacy rule). Only prompt_hash (SHA-256) is stored in events and logs.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    task_type: str
    """Delegation task type, e.g. 'changelog', 'pr_description', 'test_triage'."""

    prompt_hash: str
    """SHA-256 of prompt content. Stored in events. Raw prompt NOT persisted by default."""

    prompt: str
    """Raw prompt text. In-memory only — never persisted or emitted to Kafka by default."""

    task_id: str | None = None
    """Optional correlation ID linking this delegation to a Linear ticket or workflow."""

    max_tokens: int = 4096
    temperature: float = 0.3

    required_tier: str | None = None
    """Force a specific model tier: 'local', 'free', or 'frontier'. None = auto-select."""

    session_id: str | None = None
    repo_name: str | None = None
