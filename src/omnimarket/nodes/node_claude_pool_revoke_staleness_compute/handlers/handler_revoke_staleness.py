# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23887: pure decision of whether a recorded revoke is stale."""

from __future__ import annotations

from datetime import timedelta

from omnimarket.models.model_claude_pool_revoke_staleness import (
    ModelClearRevokeCommand,
    ModelRevokeStalenessRequest,
    ModelRevokeStalenessResult,
)


class HandlerRevokeStaleness:
    """Decides a revoke's staleness from its stamp, observations and margin."""

    def handle(
        self, request: ModelRevokeStalenessRequest
    ) -> ModelRevokeStalenessResult:
        if request.policy is None:
            return ModelRevokeStalenessResult(
                label=request.label,
                verdict="not_configured",
                reason="stale_revoke is not configured on this host: the revoke is kept",
                clear=None,
            )
        if request.revoked_at is None:
            return ModelRevokeStalenessResult(
                label=request.label,
                verdict="unknown",
                reason="the revoke carries no stamp, so nothing can be shown to come after it: kept",
                clear=None,
            )

        margin = timedelta(seconds=request.policy.margin_seconds)
        threshold = request.revoked_at + margin
        qualifying = [
            obs
            for obs in request.observations
            if obs.fingerprint == request.fingerprint and obs.at > threshold
        ]
        if not qualifying:
            return ModelRevokeStalenessResult(
                label=request.label,
                verdict="real",
                reason="no later success of this token on any host: the revoke stands",
                clear=None,
            )

        evidence = min(qualifying, key=lambda obs: (obs.at, obs.host, obs.kind))
        clear = ModelClearRevokeCommand(
            label=request.label,
            fingerprint=request.fingerprint,
            expected_revoked_at=request.revoked_at,
            evidence_host=evidence.host,
            evidence_kind=evidence.kind,
            evidence_at=evidence.at,
        )
        return ModelRevokeStalenessResult(
            label=request.label,
            verdict="stale",
            reason=(
                f"a later success of this token on {evidence.host} "
                f"({evidence.kind} at {evidence.at.isoformat()}) clears the revoke"
            ),
            clear=clear,
        )
