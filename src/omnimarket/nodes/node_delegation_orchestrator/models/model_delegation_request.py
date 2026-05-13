# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2026 OmniNode Team
"""Delegation request model for the delegation pipeline.

Represents a command to delegate a task (test, document, research)
to a local LLM via the ONEX runtime event bus.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.nodes.node_budget_policy_compute.models.model_budget_limits import (
    ModelBudgetLimits,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_contract import (
    EnumQualityContractMode,
    validate_acceptance_criteria,
)


class ModelDelegationRequest(BaseModel):
    """Delegation command: prompt, task type, and source context.

    Attributes:
        prompt: The user prompt to delegate to the local LLM.
        task_type: Classification of the delegation task.
        source_session_id: Session that originated the delegation request.
        source_file_path: File context for the delegation, if any.
        correlation_id: Unique identifier for tracking through the pipeline.
        max_tokens: Maximum tokens for the LLM response.
        emitted_at: Timestamp when the request was created.
        output_schema_key: When set, activates the compliance loop — the
            orchestrator validates each LLM response against the schema
            registered under this key in omnimarket's output schema registry
            and emits repair prompts on failure (OMN-10794).
        compliance_budget: Token / cost / time ceilings the compliance loop
            enforces between attempts. Required when ``output_schema_key`` is
            set, ignored otherwise.
        quality_contract_mode: Whether request-level acceptance criteria extend
            or replace task-class DoD.
        acceptance_criteria: Request-level quality checks to enforce at the gate.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    prompt: str = Field(
        ...,
        description="The user prompt to delegate to the local LLM.",
    )
    task_type: Literal["test", "document", "research"] = Field(
        ...,
        description="Classification of the delegation task.",
    )
    source_session_id: str | None = Field(
        default=None,
        description="Session that originated the delegation request.",
    )
    source_file_path: str | None = Field(
        default=None,
        description="File context for the delegation, if any.",
    )
    correlation_id: UUID = Field(
        ...,
        description="Unique identifier for tracking through the pipeline.",
    )
    max_tokens: int = Field(
        default=2048,
        description="Maximum tokens for the LLM response.",
    )
    emitted_at: datetime = Field(
        ...,
        description="Timestamp when the request was created.",
    )
    output_schema_key: str | None = Field(
        default=None,
        description=(
            "When set, the orchestrator runs the schema-compliance loop: it "
            "validates each inference response against the registry-resolved "
            "schema and emits repair prompts on validation failure. None = "
            "legacy single-attempt path."
        ),
    )
    compliance_budget: ModelBudgetLimits | None = Field(
        default=None,
        description=(
            "Budget ceilings (tokens, cost, elapsed time) the compliance loop "
            "enforces between repair attempts. Required when "
            "``output_schema_key`` is set."
        ),
    )
    quality_contract_mode: EnumQualityContractMode = Field(
        default="extend_task_class",
        description=(
            "How request-level acceptance criteria interact with task-class DoD."
        ),
    )
    acceptance_criteria: tuple[str, ...] = Field(
        default=(),
        description="Request-level quality checks enforced by the quality gate.",
    )

    @model_validator(mode="after")
    def _validate_compliance_loop_config(self) -> Self:
        """Reject ``output_schema_key`` set without ``compliance_budget``.

        The compliance loop's evaluator requires both. Catching this at model
        construction time prevents a runtime assertion in the workflow handler
        when the orchestrator first sees the inference response.
        """
        if self.output_schema_key is not None and self.compliance_budget is None:
            msg = (
                "compliance_budget is required when output_schema_key is set "
                "(the compliance loop has nothing to evaluate against without "
                "token / cost / time ceilings)"
            )
            raise ValueError(msg)
        validate_acceptance_criteria(self.acceptance_criteria)
        return self


__all__: list[str] = ["ModelDelegationRequest"]
