# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelOccAttestationObserveRequest — the report-only attestation-observe command (OMN-14393).

Identifies the product PR whose on-PR OCC companion should be OBSERVED (never
mutated): resolve the Evidence-Source OCC PR, byte-diff its companion against the
recomputed ``compute_companion_plan``, and check the product PR's occ-preflight
eligibility. The result is one :class:`ModelOccAutoauthorObservation` record.

REPORT-ONLY / DEFAULT-OFF: this node reads only. It opens nothing, stamps
nothing, and blocks nothing.
"""

from __future__ import annotations

from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


class ModelOccAttestationObserveRequest(BaseModel):
    """Command to observe (attest, read-only) the OCC companion on a product PR."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(..., description="Product repo slug (owner/repo).")
    pr_number: int = Field(..., description="Product PR number to observe.")
    occ_repo: str = Field(
        default="OmniNode-ai/onex_change_control", description="OCC repo slug."
    )
    runner: str = Field(
        default="node_occ_companion_compute",
        description="Receipt runner identity used to recompute the canonical plan (must differ from verifier).",
    )
    verifier: str = Field(
        default="occ-evidence-source-autobind",
        description="Receipt verifier identity used to recompute the canonical plan.",
    )
    correlation_id: UUID = Field(default_factory=uuid4)


__all__ = ["ModelOccAttestationObserveRequest"]
