# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared report content-anchor probe result models — canonical OWNER surface.

This module is the OWNER of the OMN-15164 anchor-probe RESULT-side shapes
(`EnumAnchorProbeStatus`, the per-claim `Model*ProbeResult` types, and their
container `ModelReportAnchorProbeResult`). It was promoted out of
`node_report_anchor_probe_effect`'s node-private `models` package (OMN-15163)
so `node_report_validation_compute` can consume the EXACT seam type
`node_report_anchor_probe_effect` produces without a cross-node model
reach-in (`tests/test_no_cross_node_reach_in.py`, which fails closed on any
NEW `omnimarket.nodes.<node_a>.*models*` -> `omnimarket.nodes.<node_b>`
import and forbids growing its allowlist — this repo's mechanism for "one
canonical model per shape, promote shared types to omnimarket.events.*"
rather than an advisory guideline).

`node_report_anchor_probe_effect.models.model_probe_status` /
`model_probe_outcome` / `model_probe_result` re-export these same names for
backward compatibility with that node's own intra-node imports; this module
is the canonical definition site. The claim-side input models
(`Model*AnchorClaim`, `ModelReportAnchorProbeRequest`) stay node-private —
nothing outside `node_report_anchor_probe_effect` needs them, so promoting
them would be scope creep without a reach-in to fix.
"""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class EnumAnchorProbeStatus(StrEnum):
    """Outcome of a single anchor probe (sha, path, or PR-number claim).

    Shared across all three claim kinds so a consumer (the OMN-15163 COMPUTE
    validator) can branch on one closed vocabulary instead of three
    partially-overlapping ones. ``detail`` on the owning probe-result model
    carries the free-text specifics (e.g. "resolves to a blob, not a commit").
    """

    RESOLVED = "resolved"
    NOT_RESOLVED = "not_resolved"
    NOT_FOUND = "not_found"
    NOT_A_FILE = "not_a_file"
    ESCAPES_ROOT = "escapes_root"
    MISSING_CONTEXT = "missing_context"
    LOOKUP_FAILED = "lookup_failed"


class ModelShaProbeResult(BaseModel):
    """Resolution result for one ``*_sha`` claim."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    field_name: str
    sha: str
    status: EnumAnchorProbeStatus
    detail: str = ""

    @property
    def resolved(self) -> bool:
        return self.status is EnumAnchorProbeStatus.RESOLVED


class ModelPathProbeResult(BaseModel):
    """Existence + containment result for one ``*_paths`` claim entry."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    field_name: str
    path: str
    resolved_path: str = Field(
        default="", description="Absolute path after resolve(); '' if never resolved."
    )
    status: EnumAnchorProbeStatus
    detail: str = ""

    @property
    def resolved(self) -> bool:
        return self.status is EnumAnchorProbeStatus.RESOLVED


class ModelPrProbeResult(BaseModel):
    """Confirmation result for the optional PR-number claim."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    field_name: str
    pr_number: int
    repo: str
    status: EnumAnchorProbeStatus
    state: str = Field(
        default="",
        description="gh-reported PR state (OPEN/MERGED/CLOSED), if resolved.",
    )
    detail: str = ""

    @property
    def resolved(self) -> bool:
        return self.status is EnumAnchorProbeStatus.RESOLVED


class ModelReportAnchorProbeResult(BaseModel):
    """Typed probe results for every claim in the request.

    This is the SEAM surface: `node_report_anchor_probe_effect` (OMN-15164)
    produces it; `node_report_validation_compute` (OMN-15163) consumes it as
    part of its own input. Do not rename fields here without updating both
    nodes' contract.yaml / PR bodies in the same PR.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    sha_results: tuple[ModelShaProbeResult, ...] = Field(default=())
    path_results: tuple[ModelPathProbeResult, ...] = Field(default=())
    pr_result: ModelPrProbeResult | None = Field(default=None)

    @property
    def all_resolved(self) -> bool:
        """True iff every probed claim (sha, path, and PR if present) resolved.

        An empty request (no claims at all) is vacuously ``True`` -- absence of
        a claim is not a failed claim; the caller decides whether a report
        with zero content-anchor fields is itself acceptable.
        """
        if self.pr_result is not None and not self.pr_result.resolved:
            return False
        return all(r.resolved for r in self.sha_results) and all(
            r.resolved for r in self.path_results
        )

    @property
    def total_claims(self) -> int:
        return (
            len(self.sha_results)
            + len(self.path_results)
            + (1 if self.pr_result is not None else 0)
        )


__all__ = [
    "EnumAnchorProbeStatus",
    "ModelPathProbeResult",
    "ModelPrProbeResult",
    "ModelReportAnchorProbeResult",
    "ModelShaProbeResult",
]
