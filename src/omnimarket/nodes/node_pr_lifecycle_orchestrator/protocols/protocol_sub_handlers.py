"""Protocol interfaces for pr_lifecycle sub-handlers.

Defines the contracts that each sub-handler node must satisfy when injected
into the orchestrator. Protocol-based DI allows the orchestrator to be tested
in isolation and composed with real or mock implementations.

Protocol signatures match the real sub-handler handle() signatures — the
handlers are the source of truth. The orchestrator constructs the proper
input models before calling each sub-handler.

Related:
    - OMN-8087: Create pr_lifecycle_orchestrator Node
    - OMN-8082: inventory_compute
    - OMN-8083: triage_compute
    - OMN-8084: merge_effect
    - OMN-8085: fix_effect
    - OMN-8086: state_reducer
    - OMN-9234: Fix protocol-signature drift
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Shared data models (orchestrator-internal, used between phases)
# ---------------------------------------------------------------------------


class PrRecord(BaseModel):
    """Raw PR data collected by the inventory handler."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pr_number: int = Field(..., description="GitHub PR number.")
    repo: str = Field(..., description="Repo slug, e.g. 'OmniNode-ai/omnimarket'.")
    title: str = Field(default="")
    branch: str = Field(default="")
    checks_status: str = Field(
        default="unknown",
        description="CI checks status: success | failure | pending | unknown",
    )
    review_status: str = Field(
        default="unknown",
        description="Review status: approved | changes_requested | pending | unknown",
    )
    has_conflicts: bool = Field(default=False)
    coderabbit_unresolved: int = Field(
        default=0,
        description="Count of unresolved CodeRabbit threads.",
    )
    merge_state_status: str | None = Field(
        default=None,
        description="GitHub merge state: CLEAN | DIRTY | BLOCKED | BEHIND | UNKNOWN",
    )


class EnumPrCategory(StrEnum):
    """Triage classification for a PR."""

    GREEN = "green"
    RED = "red"
    CONFLICTED = "conflicted"
    NEEDS_REVIEW = "needs_review"
    UNKNOWN = "unknown"


class TriageRecord(BaseModel):
    """A classified PR from the triage handler."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pr_number: int = Field(..., description="GitHub PR number.")
    repo: str = Field(...)
    category: EnumPrCategory = Field(default=EnumPrCategory.UNKNOWN)
    block_reason: str = Field(
        default="",
        description="Why this PR is blocked (populated for non-green PRs).",
    )


class EnumReducerIntent(StrEnum):
    """Intent emitted by the state reducer to direct the orchestrator."""

    MERGE = "merge"
    FIX = "fix"
    SKIP = "skip"


class ReducerIntent(BaseModel):
    """A single intent from the reducer for a specific PR."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pr_number: int = Field(...)
    repo: str = Field(...)
    intent: EnumReducerIntent = Field(...)
    reason: str = Field(default="")


# ---------------------------------------------------------------------------
# Handler result models (orchestrator-internal aggregates)
# ---------------------------------------------------------------------------


class InventoryResult(BaseModel):
    """Result from the inventory handler (orchestrator aggregate)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    prs: tuple[PrRecord, ...] = Field(default_factory=tuple)
    total_collected: int = Field(default=0, ge=0)


class PrTriageResult(BaseModel):
    """Result from the triage handler (orchestrator aggregate)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    classified: tuple[TriageRecord, ...] = Field(default_factory=tuple)
    green_count: int = Field(default=0, ge=0)
    non_green_count: int = Field(default=0, ge=0)


class ReducerResult(BaseModel):
    """Result from the state reducer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    intents: tuple[ReducerIntent, ...] = Field(default_factory=tuple)
    merge_count: int = Field(default=0, ge=0)
    fix_count: int = Field(default=0, ge=0)
    skip_count: int = Field(default=0, ge=0)


class MergeResult(BaseModel):
    """Result from the merge effect handler (orchestrator aggregate)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    prs_merged: int = Field(default=0, ge=0)
    prs_failed: int = Field(default=0, ge=0)


class FixResult(BaseModel):
    """Result from the fix effect handler (orchestrator aggregate)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    prs_dispatched: int = Field(default=0, ge=0)
    prs_skipped: int = Field(default=0, ge=0)


# ---------------------------------------------------------------------------
# Protocols — signatures match real sub-handler handle() methods exactly.
#
# HandlerPrLifecycleInventory.handle(input_model: ModelPrInventoryInput)
#   → ModelPrInventoryOutput
#
# HandlerPrLifecycleTriage.handle(correlation_id, prs: tuple[ModelPrInventoryItem])
#   → ModelPrTriageOutput
#
# HandlerPrLifecycleStateReducer.handle(*args, correlation_id, classified, ...)
#   → Any (ReducerResult-compatible)
#
# HandlerPrLifecycleMerge.handle(command: ModelPrMergeCommand)
#   → ModelPrMergeResult
#
# HandlerPrLifecycleFix.handle(command: ModelPrLifecycleFixCommand)
#   → ModelPrLifecycleFixResult
# ---------------------------------------------------------------------------


@runtime_checkable
class ProtocolInventoryHandler(Protocol):
    """Collect raw PR state from GitHub.

    Signature matches HandlerPrLifecycleInventory.handle().
    """

    def handle(self, input_model: Any) -> Any: ...


@runtime_checkable
class ProtocolTriageHandler(Protocol):
    """Classify collected PRs into categories.

    Signature matches HandlerPrLifecycleTriage.handle().
    """

    async def handle(
        self,
        correlation_id: UUID,
        prs: tuple[Any, ...],
    ) -> Any: ...


@runtime_checkable
class ProtocolStateReducerHandler(Protocol):
    """Pure FSM reducer: (state, triage_result, flags) -> intents[].

    Signature matches HandlerPrLifecycleStateReducer.handle() which accepts
    *args and **kwargs for dual-path dispatch (orchestrator + RuntimeLocal shim).
    """

    async def handle(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> Any: ...


@runtime_checkable
class ProtocolMergeHandler(Protocol):
    """Execute merges for PRs with MERGE intent.

    Signature matches HandlerPrLifecycleMerge.handle().
    """

    async def handle(self, command: Any) -> Any: ...


@runtime_checkable
class ProtocolFixHandler(Protocol):
    """Dispatch remediation for PRs with FIX intent.

    Signature matches HandlerPrLifecycleFix.handle().
    """

    async def handle(self, command: Any) -> Any: ...
