# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Record the hook-chain probe's verdict and surface a broken chain.

Canonical definition-B shape. A broken chain is a result, not a malformed input:
it is logged at ERROR and rides the terminal event rather than being raised.
"""

from __future__ import annotations

import logging

from omnimarket.nodes.node_hook_chain_probe_verdict_effect.models.model_hook_chain_probe_verdict import (
    ModelHookChainHealthRecorded,
    ModelHookChainProbeOutcome,
)

logger = logging.getLogger(__name__)


class HandlerHookChainProbeVerdict:
    """Judge one probe outcome."""

    def handle(
        self, request: ModelHookChainProbeOutcome
    ) -> ModelHookChainHealthRecorded:
        healthy = request.chain_complete and request.error is None
        if healthy:
            logger.info(
                "hook chain healthy (correlation_id=%s)", request.correlation_id
            )
        else:
            logger.error(
                "HOOK CHAIN BROKEN: failed_leg=%s primary_blocker=%s error=%s "
                "(correlation_id=%s)",
                request.failed_leg,
                request.primary_blocker,
                request.error,
                request.correlation_id,
            )
        return ModelHookChainHealthRecorded(
            correlation_id=request.correlation_id,
            healthy=healthy,
            failed_leg=request.failed_leg,
            primary_blocker=request.primary_blocker,
            error=request.error,
        )


__all__ = ["HandlerHookChainProbeVerdict"]
