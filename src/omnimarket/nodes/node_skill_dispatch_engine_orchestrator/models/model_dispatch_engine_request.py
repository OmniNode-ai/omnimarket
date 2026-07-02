# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelDispatchEngineRequest — input to the dispatch_engine router.

The dispatch_engine node is a THIN ROUTER over two already-real pieces:

  1. ``node_rsd_fill_compute`` (``HandlerRsdFill``) — pure RSD scoring / ranking.
  2. ``node_self_healing_dispatch_orchestrator`` — per-repo grouping + fan-out.

This request carries an already-collected candidate ticket set (scored or not —
the router re-ranks via RSD). Backlog *polling* (Linear I/O) is owned upstream by
``node_pipeline_fill``; the router deliberately does not re-implement that I/O
boundary (OMN-13834).

Related:
    - OMN-13834: dispatch_engine router rebuild
    - OMN-8688: node_pipeline_fill (backlog -> scored candidates)
    - OMN-12208: node_self_healing_dispatch_orchestrator (TeamCreate fan-out)
"""

from __future__ import annotations

from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.model_scored_ticket import ModelScoredTicket


class ModelDispatchEngineRequest(BaseModel):
    """Input to the dispatch_engine router.

    The router scores/ranks ``candidate_tickets`` via RSD, applies ``top_n`` and
    ``min_score`` cuts, then routes the survivors through the self-healing
    dispatch grouper to produce per-repo worker specs.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(
        default_factory=uuid4, description="Cycle correlation ID."
    )
    candidate_tickets: tuple[ModelScoredTicket, ...] = Field(
        default_factory=tuple,
        description="Candidate tickets to route (RSD-scored by the router).",
    )
    repo_hints: dict[str, str] = Field(
        default_factory=dict,
        description="Optional ticket_id -> repo name mapping for fan-out grouping.",
    )
    top_n: int = Field(
        default=5, ge=1, le=20, description="Maximum tickets to route per cycle."
    )
    min_score: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Minimum RSD score required to route a ticket.",
    )
    dry_run: bool = Field(
        default=False,
        description="Score and group without marking the run as dispatched.",
    )


__all__ = ["ModelDispatchEngineRequest"]
