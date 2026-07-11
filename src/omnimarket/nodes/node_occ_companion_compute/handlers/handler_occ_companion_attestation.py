# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerOccCompanionAttestation — the OMN-14055 attestation oracle (RSD-5 primitive).

The verify-side extension of RSD-1 (`node_occ_companion_compute`). Given the
companion files as they exist on a candidate PR/receipt set (``observed_files``)
and the SAME ``ModelOccCompanionRequest`` that reconstructs the machine-observed
facts for that PR, it recomputes the canonical plan via ``compute_companion_plan``
and byte-diffs ``deterministic_fingerprint`` against the observed files.

This IS the "mechanically rejects implementer-authored companions" primitive
OMN-14055 requires: the verdict is a pure function of CONTENT reproducibility,
never of identity fields. A hand-authored companion that fabricates
``runner``/``verifier`` strings matching the canonical producer still fails —
proving actor-identity-only checks are insufficient (adversarial finding §4.1 of
``docs/plans/2026-07-10-occ-autogen-mechanization-design.md``: all agents share
one GitHub identity, so identity-based provenance cannot distinguish autogen
from hand-authored). An independent-verifier-authored companion is NOT a bypass
of this check — it is still required to be byte-reproducible from the same
machine-observed facts; the escape hatch is about WHO may author the request's
identity fields, not about skipping the digest comparison.

Scope note (2026-07-11): this handler is standalone and NOT wired into any CI
gate in this PR. Wiring it as a fail-closed receipt-gate/occ-preflight check is
deferred to a follow-up ticket, blocked on the RSD-2/3 EFFECT nodes making
``node_occ_companion_compute`` the live producer — turning this into a blocking
gate today would reject essentially all current OCC-companion traffic, since
nothing in production yet emits a reproducible companion (the born-path
producer today is still the bespoke ``OccAutobindAdapter``, which never calls
this node).
"""

from __future__ import annotations

import logging
from typing import Literal
from uuid import UUID

from omnimarket.nodes.node_occ_companion_compute.handlers.handler_occ_companion_compute import (
    compute_companion_plan,
    deterministic_fingerprint,
)
from omnimarket.nodes.node_occ_companion_compute.models.model_occ_attestation_result import (
    ModelOccAttestationRequest,
    ModelOccAttestationResult,
)
from omnimarket.nodes.node_occ_companion_compute.models.model_occ_companion_plan import (
    ModelCompanionFile,
)
from omnimarket.nodes.node_occ_companion_compute.models.model_occ_companion_request import (
    ModelOccCompanionRequest,
)

logger = logging.getLogger(__name__)


def verify_companion_attestation(
    observed_files: tuple[ModelCompanionFile, ...],
    request: ModelOccCompanionRequest,
) -> ModelOccAttestationResult:
    """Byte-diff observed companion files against the recomputed canonical plan.

    PURE — zero I/O (recomputes via ``compute_companion_plan``, which is itself
    zero-I/O). Returns ``accepted=True`` only when
    ``deterministic_fingerprint(observed_files)`` equals the ``deterministic_digest``
    of the plan recomputed from ``request``. Any deviation — a hand-authored
    companion, a stale companion whose contract/receipt hashes have since moved,
    or a tampered field — is rejected with an actionable reason.
    """
    recomputed = compute_companion_plan(request)
    observed_digest = deterministic_fingerprint(observed_files)
    expected_digest = recomputed.deterministic_digest

    if observed_digest == expected_digest:
        return ModelOccAttestationResult(
            accepted=True,
            observed_digest=observed_digest,
            expected_digest=expected_digest,
            reason=(
                "ACCEPTED: observed companion is byte-reproducible from "
                f"compute_companion_plan for {request.repo}#{request.pr_number}."
            ),
        )

    return ModelOccAttestationResult(
        accepted=False,
        observed_digest=observed_digest,
        expected_digest=expected_digest,
        reason=(
            "REJECTED: observed companion files are not reproducible from "
            f"compute_companion_plan for {request.repo}#{request.pr_number} "
            f"(observed_digest={observed_digest!r} != "
            f"expected_digest={expected_digest!r}). A hand-authored, tampered, or "
            "stale companion never byte-matches the canonical COMPUTE output; "
            "rerun the producer to regenerate it."
        ),
    )


class HandlerOccCompanionAttestation:
    """Pure COMPUTE handler: verify companion reproducibility (zero I/O)."""

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["COMPUTE"]:
        return "COMPUTE"

    async def handle(
        self,
        correlation_id: UUID,
        request: ModelOccAttestationRequest,
    ) -> ModelOccAttestationResult:
        """Verify ``request.observed_files`` against the recomputed canonical plan."""
        logger.info(
            "occ_companion_attestation: repo=%s pr=%s correlation_id=%s",
            request.expected.repo,
            request.expected.pr_number,
            correlation_id,
        )
        return verify_companion_attestation(request.observed_files, request.expected)


__all__ = [
    "HandlerOccCompanionAttestation",
    "verify_companion_attestation",
]
