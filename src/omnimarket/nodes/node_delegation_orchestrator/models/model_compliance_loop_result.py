# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Result model for one iteration of the schema-repair compliance loop — OMN-10792.

A single call to :class:`HandlerComplianceLoop` evaluates one LLM attempt against
the target output schema and returns either:

* ``compliant=True`` — the LLM output validated; the orchestrator can finalize
  the delegation and forward the cumulative ``tokens_to_compliance`` and
  ``compliance_attempts`` to the downstream event.
* ``compliant=False`` with ``budget_action == CONTINUE`` — emit ``repair_prompt``
  to a fresh inference attempt; the orchestrator increments ``compliance_attempts``
  and adds the next attempt's tokens to the running total.
* ``compliant=False`` with ``budget_action == ABORT`` — stop; no further attempts.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_budget_policy_compute.models.model_budget_policy_enums import (
    EnumBudgetAction,
)


class ModelComplianceLoopResult(BaseModel):
    """One-shot outcome of evaluating a single LLM attempt for schema compliance."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    compliant: bool = Field(
        ...,
        description="True if the candidate output validated against the target schema.",
    )
    validated_output: str = Field(
        default="",
        description=(
            "The candidate output, exactly as supplied. Empty when the schema "
            "lookup failed before validation could run."
        ),
    )
    tokens_to_compliance: int = Field(
        default=0,
        ge=0,
        description=(
            "Total tokens consumed across all attempts so far (including this one)."
        ),
    )
    compliance_attempts: int = Field(
        default=1,
        ge=1,
        description=(
            "Number of LLM attempts evaluated so far (including this one). The "
            "first call to the loop is always attempt 1."
        ),
    )
    repair_prompt: str = Field(
        default="",
        description=(
            "Prompt to feed back to the LLM for the next attempt when "
            "``compliant`` is False and ``budget_action`` is CONTINUE. Empty "
            "when the loop terminates (compliant or aborted)."
        ),
    )
    budget_action: EnumBudgetAction = Field(
        default=EnumBudgetAction.CONTINUE,
        description=(
            "Result of the budget-policy check after this attempt. CONTINUE "
            "means the orchestrator may issue another repair attempt; ABORT "
            "means it must stop."
        ),
    )
    abort_reason: str = Field(
        default="",
        description=(
            "Human-readable explanation when ``budget_action`` is ABORT or when "
            "the loop terminated without compliance for another reason "
            "(unknown schema key, schema-repair returned non-repairable, etc.)."
        ),
    )


__all__ = ["ModelComplianceLoopResult"]
