# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelDispatchEngineReceipt — output of the dispatch_engine router.

A REAL dispatch receipt (not a ``"dispatched"`` placeholder string): it names the
run, echoes the RSD-scored/ranked candidates that survived the cuts, and carries
the concrete per-repo worker specs produced by routing through the self-healing
dispatch grouper (OMN-13834).

Related:
    - OMN-13834: dispatch_engine router rebuild
    - OMN-12208: node_self_healing_dispatch_orchestrator
"""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.model_scored_ticket import ModelScoredTicket


class EnumDispatchEngineStatus(StrEnum):
    """Terminal status of a dispatch_engine routing cycle."""

    DISPATCHED = "dispatched"
    """A live dispatcher was injected and launched the grouped workers."""

    PLANNED = "planned"
    """Candidates routed into concrete worker specs; live launch delegated to
    the self_healing_dispatch runtime adapter (no dispatcher injected)."""

    DRY_RUN = "dry_run"
    """Scored + grouped only; the caller requested a dry run."""

    NO_CANDIDATES = "no_candidates"
    """No candidate survived the top_n / min_score cuts — nothing to dispatch."""


class ModelDispatchWorkerSpec(BaseModel):
    """A concrete per-repo worker assignment produced by the fan-out grouper."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    worker_name: str = Field(description="Deterministic worker name for this group.")
    repo: str = Field(description="Target repo name (e.g. 'omniclaude').")
    ticket_ids: tuple[str, ...] = Field(
        description="Ordered ticket IDs assigned to this worker."
    )


class ModelDispatchEngineReceipt(BaseModel):
    """Terminal receipt for one dispatch_engine routing cycle."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(description="Stable run identifier for this cycle.")
    correlation_id: UUID = Field(description="Cycle correlation ID.")
    status: EnumDispatchEngineStatus = Field(description="Terminal routing status.")
    scored_candidates: tuple[ModelScoredTicket, ...] = Field(
        default=(),
        description="RSD-ranked candidates that survived the top_n / min_score cuts.",
    )
    worker_specs: tuple[ModelDispatchWorkerSpec, ...] = Field(
        default=(),
        description="Per-repo worker specs the run routed to (fan-out plan).",
    )
    total_candidates: int = Field(
        default=0, ge=0, description="Total candidate tickets supplied to the router."
    )
    total_selected: int = Field(
        default=0, ge=0, description="Candidates that survived the cuts."
    )
    dry_run: bool = Field(
        default=False, description="True when the caller requested a dry run."
    )


__all__ = [
    "EnumDispatchEngineStatus",
    "ModelDispatchEngineReceipt",
    "ModelDispatchWorkerSpec",
]
