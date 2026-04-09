# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request model for the overseer verifier node.

Wraps a TaskStateEnvelope with optional overseer model output fields
for deterministic verification.

Related:
    - OMN-8031: node_overseer_verifier in omnimarket
    - OMN-8025: Overseer seam integration epic
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ModelVerifierRequest(BaseModel):
    """Input to the overseer verifier.

    Contains the task state envelope to verify, along with optional
    model output metadata used by the allowed_action_scope and
    contract_compliance checks.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str = Field(
        ..., description="Unique identifier for the task being verified."
    )
    status: str = Field(..., description="Current task status string.")
    domain: str = Field(..., description="Domain the task is running in.")
    node_id: str = Field(..., description="Node ID that produced the output.")
    runner_id: str | None = Field(
        default=None, description="Runner that executed the task."
    )
    attempt: int = Field(default=1, ge=1, description="Attempt number.")
    payload: dict[str, Any] = Field(
        default_factory=dict,
        description="Task payload for completeness and scope checks.",
    )
    error: str | None = Field(
        default=None, description="Error message if task has failed."
    )
    # Model output metadata (optional — populated by overseer after LLM run)
    confidence: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Confidence score from the model output (0.0-1.0).",
    )
    cost_so_far: float | None = Field(
        default=None,
        description="Cumulative cost in USD at the time of verification. Validity checked by handler.",
    )
    allowed_actions: list[str] = Field(
        default_factory=list,
        description="Action names the model claimed to take — checked against allowed scope.",
    )
    declared_invariants: list[str] = Field(
        default_factory=list,
        description="Invariant assertions from the model output.",
    )
    schema_version: str = Field(
        default="1.0",
        description="Schema version of the request envelope.",
    )


__all__: list[str] = ["ModelVerifierRequest"]
