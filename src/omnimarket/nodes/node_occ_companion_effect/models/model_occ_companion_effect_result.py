# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelOccCompanionEffectResult — the RSD-3 write-EFFECT outcome (OMN-14622).

What the write-EFFECT observed/did: the cited tickets, the OCC companion PR it
opened (or would open, in dry_run), the deterministic reproducibility digest the
compute plan produced, and whether the product PR body was stamped with the
``Evidence-Source`` block. ``no_op``/``fast_path`` mirror the compute plan's own
early-exit reasons so a caller can tell "nothing to author" from "authored".
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelOccCompanionEffectResult(BaseModel):
    """The outcome of an OCC companion write-EFFECT run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(..., description="Product repo slug.")
    pr_number: int = Field(..., description="Product PR number.")
    mode: Literal["dry_run", "mutate"] = Field(..., description="Run mode.")
    action: str = Field(..., description="Human-readable summary of what happened.")

    no_op: bool = Field(
        default=False, description="True when the plan had nothing to author."
    )
    no_op_reason: str = Field(default="", description="Why nothing was authored.")
    suppression_code: str = Field(
        default="",
        description="Machine-readable declining-branch identity from the compute "
        "plan (OMN-16665), empty when the plan authored something.",
    )
    suppression_surfaced: bool = Field(
        default=False,
        description="True when the decline was surfaced back onto the product PR "
        "(idempotent comment + mint-status check-run). False on a re-trigger whose "
        "identical decline was already surfaced, and in dry_run.",
    )
    fast_path: bool = Field(
        default=False, description="True when the trivial-infra fast-path skipped it."
    )

    tickets: tuple[str, ...] = Field(
        default=(), description="The gate-parity cited ticket set."
    )
    occ_branch: str = Field(
        default="", description="Deterministic OCC companion branch."
    )
    occ_pr_number: int | None = Field(
        default=None, description="OCC companion PR number (opened or reused)."
    )
    occ_pr_url: str = Field(default="", description="OCC companion PR HTML URL.")
    product_body_stamped: bool = Field(
        default=False,
        description="True when the product PR body was patched with the Evidence-Source block.",
    )
    companion_paths: tuple[str, ...] = Field(
        default=(), description="Paths of the net-new OCC companion files in the plan."
    )
    deterministic_digest: str = Field(
        default="",
        description="The compute plan's reproducibility fingerprint (attestation-oracle digest).",
    )
    wedges: tuple[str, ...] = Field(
        default=(),
        description="Self-reported authoring-defect wedge codes from the plan.",
    )


__all__ = ["ModelOccCompanionEffectResult"]
