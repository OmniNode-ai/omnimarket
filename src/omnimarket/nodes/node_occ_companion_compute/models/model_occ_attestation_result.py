# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelOccAttestationRequest / ModelOccAttestationResult — the OMN-14055 oracle seam.

The verify-side extension of RSD-1 (`node_occ_companion_compute`). The oracle
recomputes `compute_companion_plan` from machine-observed facts and byte-diffs
`deterministic_fingerprint` against the companion files as OBSERVED (on an OCC
PR, or any candidate receipt set) — never against identity fields. This is the
regression proof that actor-identity-only checks are insufficient (§4.1 of
`docs/plans/2026-07-10-occ-autogen-mechanization-design.md`): a hand-authored
companion under the SAME `runner`/`verifier` strings as the canonical producer
still fails this check if its bytes are not the COMPUTE node's output.

Standalone in this PR — not yet wired into any CI gate. Wiring this into
receipt-gate/occ-preflight as a fail-closed blocking gate is deferred to a
follow-up ticket, gated on the RSD-2/3 EFFECT nodes making the mechanized
producer live (a fail-closed gate today would reject ~100% of current traffic,
since nothing in production yet emits a `node_occ_companion_compute`-reproducible
companion).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_occ_companion_compute.models.model_occ_companion_plan import (
    ModelCompanionFile,
)
from omnimarket.nodes.node_occ_companion_compute.models.model_occ_companion_request import (
    ModelOccCompanionRequest,
)


class ModelOccAttestationRequest(BaseModel):
    """Input to the attestation oracle: observed files + the request to recompute from."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    observed_files: tuple[ModelCompanionFile, ...] = Field(
        ...,
        description="Companion files as they exist on the candidate PR/receipt "
        "set — the thing being attested, NOT necessarily this handler's own output.",
    )
    expected: ModelOccCompanionRequest = Field(
        ...,
        description="The machine-observed facts (PR + OCC state) the canonical "
        "plan is recomputed from. Identity fields (runner/verifier) on this "
        "request carry no weight in the verdict — only content reproducibility does.",
    )


class ModelOccAttestationResult(BaseModel):
    """The oracle's verdict: is `observed_files` this handler's own output?"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    accepted: bool = Field(
        ..., description="True iff observed_digest == the recomputed expected_digest."
    )
    observed_digest: str = Field(
        ..., description="deterministic_fingerprint() of the observed files."
    )
    expected_digest: str = Field(
        ...,
        description="deterministic_digest of the plan recomputed from `expected`.",
    )
    reason: str = Field(..., description="Operator-facing accept/reject explanation.")


__all__ = [
    "ModelOccAttestationRequest",
    "ModelOccAttestationResult",
]
