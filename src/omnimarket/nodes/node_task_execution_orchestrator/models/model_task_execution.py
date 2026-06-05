# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Boundary models for node_task_execution_orchestrator.

The orchestrator COMPOSES existing authorities; it must NOT become a new
authority. These models are a thin route boundary only:

- ``ModelTaskExecutionRequest`` wraps an arbitrary coding prompt OR a fully
  formed ``ModelTaskContract`` (reused verbatim from omnibase_core), plus
  dispatch flags. It does NOT duplicate ``ModelTaskContract`` / DoD models.
- ``ModelRouteDecision`` records which existing route NAME a requirement or
  mechanical check maps to, without executing it.
- ``ModelTaskExecutionResult`` is the V1 terminal payload: the normalized
  ``ModelTaskContract`` plus the deterministic route plan, with no side
  effects performed.

OMN-12702 (first vertical slice of the generic ``task.execute`` route).
"""

from __future__ import annotations

from enum import StrEnum

from omnibase_core.models.task.model_task_contract import ModelTaskContract
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.events.verification import (
    ModelVerificationReceipt,
)


class EnumTaskRoute(StrEnum):
    """Existing route NAMES that task.execute composes.

    These name existing node authorities. task.execute plans which route a
    requirement / mechanical check maps to; the named route owns execution.
    """

    DELEGATION = "delegation"
    VERIFICATION = "verification"


class EnumRouteItemKind(StrEnum):
    """What kind of task-contract element produced a route decision."""

    REQUIREMENT = "requirement"
    MECHANICAL_CHECK = "mechanical_check"


class ModelRouteDecision(BaseModel):
    """One deterministic mapping from a task-contract element to a route name.

    No execution happens here — this records the planned target only.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    kind: EnumRouteItemKind = Field(
        description="Whether this decision came from a requirement or a DoD check.",
    )
    source: str = Field(
        description="The requirement text or check criterion that was mapped.",
    )
    route: EnumTaskRoute = Field(
        description="Existing route NAME this element is planned to dispatch to.",
    )
    detail: str = Field(
        default="",
        description="Deterministic explanation of why this route was chosen.",
    )


class ModelTaskExecutionRequest(BaseModel):
    """Route boundary input: a raw prompt OR a fully formed task contract.

    Exactly one of ``prompt`` / ``task_contract`` must be supplied. Supplying
    both, or neither, is a deterministic validation failure (no silent default).
    """

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    prompt: str | None = Field(
        default=None,
        description="Raw coding/mechanical task prompt to normalize into a contract.",
    )
    task_contract: ModelTaskContract | None = Field(
        default=None,
        description="Fully formed task contract (reused verbatim; never duplicated).",
    )
    dry_run: bool = Field(
        default=True,
        description="V1 supports dry-run only: plan routes, perform NO side effects.",
    )
    allowed_side_effects: tuple[str, ...] = Field(
        default=(),
        description="Side-effect allowances; empty in the first vertical slice.",
    )
    target_repo: str | None = Field(
        default=None,
        description="Optional target repo applied when normalizing a prompt.",
    )
    ticket_id: str | None = Field(
        default=None,
        description="Optional parent ticket applied when normalizing a prompt.",
    )
    execute_mechanical_checks: bool = Field(
        default=False,
        description=(
            "When True, dispatch the contract's mechanical DoD checks to "
            "node_verification_receipt_generator (the execution authority) and "
            "aggregate its receipt unchanged. This is read-only verification, "
            "not a code/PR side effect, so it is permitted under dry_run."
        ),
    )
    worktree_path: str = Field(
        default="",
        description=(
            "Worktree the verification node runs mechanical checks in; passed "
            "through unchanged. Empty runs checks in the current directory."
        ),
    )


class ModelTaskExecutionResult(BaseModel):
    """V1 terminal payload: normalized contract + deterministic route plan.

    Side effects are NOT performed in the first vertical slice. ``ok`` is True
    when planning succeeded for every contract element; an unsupported element
    yields ``ok=False`` with a typed ``failure_reason`` (never a silent skip).
    """

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    ok: bool = Field(
        description="True iff every requirement and DoD check mapped to a route.",
    )
    dry_run: bool = Field(
        description="Echoes the request mode; V1 is always dry-run.",
    )
    task_contract: ModelTaskContract = Field(
        description="Normalized task contract (created from prompt or passed through).",
    )
    contract_fingerprint: str = Field(
        description="Deterministic fingerprint of the normalized contract.",
    )
    route_plan: tuple[ModelRouteDecision, ...] = Field(
        description="Deterministic, ordered route decisions for this contract.",
    )
    verification_receipt: ModelVerificationReceipt | None = Field(
        default=None,
        description=(
            "Receipt returned UNCHANGED by node_verification_receipt_generator "
            "when mechanical checks were executed. Aggregated additively; "
            "task.execute never transforms or reinterprets its pass/fail."
        ),
    )
    failure_reason: str | None = Field(
        default=None,
        description="Typed deterministic reason when an action is unsupported.",
    )


# ``from __future__ import annotations`` defers the ModelVerificationReceipt
# annotation to a string; rebuild so Pydantic resolves the imported model.
ModelTaskExecutionResult.model_rebuild()


__all__ = [
    "EnumRouteItemKind",
    "EnumTaskRoute",
    "ModelRouteDecision",
    "ModelTaskExecutionRequest",
    "ModelTaskExecutionResult",
]
