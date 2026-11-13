# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19998: pure decision of whether a runner-written revoke is real or a challenge."""

from __future__ import annotations

from omnimarket.models.model_claude_pool_revoke_cause import (
    ModelRevokeCauseRequest,
    ModelRevokeCauseResult,
)


class HandlerRevokeCause:
    """Decides, per runner-written revoke, whether it is a revoke or a challenge."""

    def handle(self, request: ModelRevokeCauseRequest) -> ModelRevokeCauseResult:
        evidence = request.evidence
        if evidence is None or not evidence.strip():
            return ModelRevokeCauseResult(
                label=request.label,
                cause="revoked",
                reason=(
                    "no evidence text came with the mark: the revoke is recorded "
                    "as a revoke"
                ),
            )
        if not request.challenge_markers:
            return ModelRevokeCauseResult(
                label=request.label,
                cause="revoked",
                reason=(
                    "no challenge markers are declared on this host: the revoke "
                    "is recorded as a revoke"
                ),
            )
        lowered = evidence.lower()
        found = sum(
            1 for marker in request.challenge_markers if marker.lower() in lowered
        )
        if found > 0:
            plural = "marker" if found == 1 else "markers"
            return ModelRevokeCauseResult(
                label=request.label,
                cause="challenged",
                reason=(
                    f"the evidence carries {found} declared challenge {plural}: "
                    "the mark is a challenge, not a real revoke"
                ),
            )
        return ModelRevokeCauseResult(
            label=request.label,
            cause="revoked",
            reason=(
                "the evidence carries no declared challenge marker: the revoke "
                "is recorded as a revoke"
            ),
        )
