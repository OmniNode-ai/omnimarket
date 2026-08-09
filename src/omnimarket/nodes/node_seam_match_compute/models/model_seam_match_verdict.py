# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Output models for ``node_seam_match_compute`` (OMN-15763).

The RSD regeneration-boundary rule (2026-08-08 methodology, §0.3): an edge is
regenerable ONLY when it carries an executable golden driving the real seam —
a contract.yaml-vs-contract.yaml shape comparison does not count. That bar is
encoded structurally here: ``EnumSeamRegenerabilityClass.REGENERABLE``
requires all three legs green; a shape-only match (leg 1 only) is classified
``SHAPE_ONLY`` and is never counted as regenerable, and the regenerable count
is always reported separately from the MATCHED count (AC8).
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "EnumSeamMatchVerdict",
    "EnumSeamRegenerabilityClass",
    "ModelSeamLegResult",
    "ModelSeamMatchVerdict",
    "ModelSeamStaleProofCheck",
]


class EnumSeamMatchVerdict(StrEnum):
    """Leg-1 classification of a seam edge."""

    UNMATCHED = "UNMATCHED"
    MISMATCH = "MISMATCH"
    MATCHED = "MATCHED"


class EnumSeamRegenerabilityClass(StrEnum):
    """Whether a MATCHED edge is backed by a real executor (§0.3 bar)."""

    REGENERABLE = "REGENERABLE"
    SHAPE_ONLY = "SHAPE_ONLY"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class ModelSeamLegResult(BaseModel):
    """Result of one leg of the three-leg composition.

    ``passed=None`` means the leg was not evaluated (e.g. no observed
    projection was supplied) — distinct from ``passed=False`` (evaluated and
    failed). Only an explicit ``True`` on all three legs earns REGENERABLE.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    passed: bool | None
    mismatching_field_path: str | None = None


class ModelSeamStaleProofCheck(BaseModel):
    """Stale-proof detector result: does the pinned registry hash still
    match the current declared-producer projection's canonical hash?

    A pin mismatch means the seam changed underneath a golden/allowlist
    entry without a re-pin — the proof is stale, not merely "no proof yet".
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    edge_id: str = Field(min_length=1)
    pinned_hash: str = Field(min_length=1)
    current_hash: str = Field(min_length=1)
    stale: bool
    detail: str = Field(min_length=1)


class ModelSeamMatchVerdict(BaseModel):
    """Full seam-match verdict for one edge."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    edge_id: str = Field(min_length=1)
    verdict: EnumSeamMatchVerdict
    regenerability: EnumSeamRegenerabilityClass
    leg1_declared_vs_declared: ModelSeamLegResult
    leg2_observed_producer_vs_declared: ModelSeamLegResult
    leg3_observed_consumer_vs_declared: ModelSeamLegResult
    declared_producer_hash: str | None = None
    declared_consumer_hash: str | None = None
    # Populated only when the request supplied both a pinned_hash and a
    # declared_producer to check it against; None (not a false "not stale")
    # when there was nothing to check.
    stale_proof: ModelSeamStaleProofCheck | None = None
