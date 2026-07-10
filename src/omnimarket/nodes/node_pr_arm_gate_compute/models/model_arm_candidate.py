# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelArmCandidate — genuine tri-state facts about a merge-ready PR.

OMN-14151 (corrected arm-gate design). Every fact here is tri-state: a real
positive/negative value, or ``None`` for "unknown/not collected". The gate
treats ``None`` exactly like an unfavorable value — WITHHOLD, never a silent
default to the favorable case. This is what closes the "draft/thread-blocked
PR forges GREEN" hole: before this node, a missing fact defaulted to a shape
that satisfied downstream checks (e.g. ``coderabbit_unresolved`` defaulting to
``0`` on a PR nobody ever queried CodeRabbit threads for).

``occ_companion_verified`` is populated by an EFFECT (a live gh/remote
read-back, e.g. the orchestrator's OCC-stamp read-back gate) and is consumed
here as a plain fact — this pure COMPUTE node never re-derives it.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelArmCandidate(BaseModel):
    """Genuine, tri-state facts about one merge-intent PR."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(..., description="Repo slug, e.g. 'OmniNode-ai/omnimarket'.")
    pr_number: int = Field(..., description="GitHub PR number.")
    is_draft: bool | None = Field(
        default=None,
        description="True/False from a genuine read; None means unknown -> WITHHOLD.",
    )
    coderabbit_unresolved: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Count of unresolved CodeRabbit review threads. None means the "
            "count was never collected -> WITHHOLD (never defaults to 0)."
        ),
    )
    merge_state_status: str | None = Field(
        default=None,
        description="GitHub mergeStateStatus: CLEAN | DIRTY | BLOCKED | BEHIND | UNKNOWN | None.",
    )
    status_checks: str | None = Field(
        default=None,
        description="Positively collected CI rollup: SUCCESS | FAILURE | PENDING | None.",
    )
    occ_companion_verified: bool | None = Field(
        default=None,
        description=(
            "EFFECT-computed fact (live read-back) proving a verified OCC "
            "evidence companion is attached to this PR. Never re-derived here."
        ),
    )
    priority_hint: int = Field(
        default=0,
        description=(
            "Caller-supplied ordering hint for wave-cap selection among "
            "multiple ARM-eligible candidates in one pass; lower arms first."
        ),
    )


__all__: list[str] = ["ModelArmCandidate"]
