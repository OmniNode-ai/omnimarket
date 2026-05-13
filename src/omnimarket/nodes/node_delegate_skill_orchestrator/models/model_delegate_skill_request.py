# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Consumer-facing delegation request model.

Distinct from the runtime-internal ``ModelDelegationRequest`` in omnibase_infra:
consumers supply ``source``, ``cwd``, ``wait``, and ``metadata`` and never set
the runtime-internal ``emitted_at`` / ``output_schema_key`` / ``compliance_budget``.
The ``task_type`` Literal is the MVP taxonomy and must match the contract.yaml
``allowed_task_types`` field.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_contract import (
    EnumQualityContractMode,
    validate_acceptance_criteria,
)


class ModelDelegateSkillRequest(BaseModel):
    """Typed delegation request from a registered adapter source."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    prompt: str = Field(..., min_length=1, description="User prompt to delegate.")
    task_type: Literal["test", "document", "research"] = Field(
        ...,
        description=(
            "Task classification for routing. MVP taxonomy; must match contract "
            "allowed_task_types."
        ),
    )
    source: Literal["claude-code", "codex"] = Field(
        ...,
        description="Registered adapter source.",
    )
    cwd: str | None = Field(default=None, description="Working directory context.")
    wait: bool = Field(default=True, description="Wait for synchronous result.")
    max_tokens: int = Field(default=2048, gt=0, le=16384)
    correlation_id: UUID = Field(default_factory=uuid4)
    metadata: dict[str, str] = Field(default_factory=dict)
    quality_contract_mode: EnumQualityContractMode = Field(
        default="extend_task_class",
        description=(
            "How request-level acceptance criteria interact with task-class DoD."
        ),
    )
    acceptance_criteria: tuple[str, ...] = Field(
        default=(),
        description=(
            "Request-level quality criteria validated before dispatch and enforced "
            "by the delegation quality gate."
        ),
    )

    @field_validator("acceptance_criteria")
    @classmethod
    def _validate_supported_acceptance_criteria(
        cls, criteria: tuple[str, ...]
    ) -> tuple[str, ...]:
        return validate_acceptance_criteria(criteria)


__all__: list[str] = [
    "EnumQualityContractMode",
    "ModelDelegateSkillRequest",
]
