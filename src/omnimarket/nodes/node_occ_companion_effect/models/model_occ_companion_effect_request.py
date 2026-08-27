# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelOccCompanionEffectRequest — the RSD-3 write-EFFECT command (OMN-14622).

The minimal command the write-EFFECT + orchestrator (``node_occ_companion_effect``)
consumes to run the full deterministic OCC-companion producer cycle end-to-end:
read (``node_occ_state_effect``, RSD-2) -> compute (``node_occ_companion_compute``,
RSD-1) -> write (this node). Every companion byte is a pure function of the
compute plan; this node owns only the git/gh side effects.

``mode`` defaults to ``dry_run`` (fail-safe): a bare invocation reads + computes
the plan and reports it WITHOUT cloning, pushing, opening a PR, or stamping the
product body. The live trigger sets ``mode="mutate"``.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


class ModelOccCompanionEffectRequest(BaseModel):
    """Command to author (or dry-run) the deterministic OCC companion for a PR."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(..., description="Product repo slug (owner/repo).")
    pr_number: int = Field(
        ..., description="Product PR number to author a companion for."
    )
    occ_repo: str = Field(
        default="OmniNode-ai/onex_change_control", description="OCC repo slug."
    )
    runner: str = Field(
        default="node_occ_companion_compute",
        description="Receipt runner identity (must differ from verifier — OMN-12791).",
    )
    verifier: str = Field(
        default="occ-evidence-source-autobind",
        description="Receipt verifier identity (must differ from runner).",
    )
    mode: Literal["dry_run", "mutate"] = Field(
        default="dry_run",
        description="'dry_run' reads+computes the plan only; 'mutate' clones, pushes, "
        "opens the OCC PR, and stamps the product body.",
    )
    allow_merged_replay: bool = Field(
        default=False,
        description="OMN-16665 merged-PR recovery override. Default False: the "
        "born path never authors a companion for a PR that is no longer open. Set "
        "True ONLY on a deliberate manual replay for a PR that MERGED without a "
        "companion (the merge/queue-latency race), to author the evidence record "
        "the race destroyed. It has no effect on a closed-UNMERGED PR — that "
        "decline (occ#4333) stands regardless.",
    )
    correlation_id: UUID = Field(default_factory=uuid4)


__all__ = ["ModelOccCompanionEffectRequest"]
