# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Start command for the runtime-closeout orchestrator (OMN-13413).

The orchestrator's bus entrypoint. Carries the lane + proof-set the closeout
runs against. ``promote`` is the operator's intent to push the artifact down the
lane ladder (dev -> stability -> prod); prod promotion stays operator-gated and
is enforced downstream by ``node_redeploy_orchestrator``'s prod-promotion gate,
not relaxed here. Shared closeout domain types live in
``omnimarket.events.runtime_closeout``.
"""

from __future__ import annotations

from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.events.runtime_closeout import EnumProofSet
from omnimarket.events.runtime_deployment import EnumRedeployScope, EnumRuntimeLane


class ModelCloseoutStartCommand(BaseModel):
    """Start a one-dispatch runtime closeout via the orchestrator.

    ``correlation_id`` defaults when absent so the typed command validates against
    the runtime-injected envelope correlation_id on the canonical ``onex
    run-node`` dispatch path (mirrors ``ModelIntegrationSweepOrchestratorRequest``
    / ``ModelCloseoutVerifyRequest``).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(
        default_factory=uuid4, description="Closeout run correlation ID."
    )
    runtime_lane: EnumRuntimeLane = Field(
        default=EnumRuntimeLane.DEV, description="Target runtime lane for the closeout."
    )
    proof_set: EnumProofSet = Field(
        default=EnumProofSet.REQUIRED,
        description="Which slice of the proof matrix to prove (required | full).",
    )
    promote: bool = Field(
        default=False,
        description=(
            "Operator intent to promote the proven artifact down the lane ladder. "
            "Prod promotion stays operator-gated downstream (redeploy prod gate); "
            "this flag never relaxes that gate."
        ),
    )
    scope: EnumRedeployScope = Field(
        default=EnumRedeployScope.FULL,
        description="Rebuild scope threaded into the deploy phase.",
    )
    git_ref: str = Field(
        default="origin/main",
        description="Git ref threaded into the deploy phase.",
    )
    image_digest: str | None = Field(
        default=None,
        description="Pinned image digest. Required for a prod promotion deploy.",
    )
    promotion_batch_id: str | None = Field(
        default=None,
        description="Promotion batch shared with OCC evidence; required for prod.",
    )
    rollback_target: str | None = Field(
        default=None,
        description="Known previous-good digest recorded in the receipt rollback plan.",
    )
    requested_by: str = Field(
        default="node_runtime_closeout_orchestrator",
        description="Identity label emitted in downstream commands.",
    )


__all__: list[str] = ["ModelCloseoutStartCommand"]
