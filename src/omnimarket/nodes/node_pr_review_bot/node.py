# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerPrReviewBot — FSM entry point for the PR Review Bot WorkflowPackage.

Contract: src/omnimarket/nodes/node_pr_review_bot/contract.yaml

Phases: INIT -> FETCH_DIFF -> REVIEW -> POST_THREADS -> WATCH ->
        JUDGE_VERIFY -> REPORT -> DONE
Circuit breaker: 3 consecutive failures -> FAILED.

Topics are declared in contract.yaml (event_bus section) and read at runtime
by the dispatch engine — they are never hardcoded here.

Full FSM logic is implemented in handlers/handler_fsm.py (OMN-7966).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class HandlerPrReviewBot:
    """FSM state machine entry point for the PR Review Bot.

    Pure COMPUTE — no external I/O. Callers wire event bus publish/subscribe
    via the contract.yaml event_bus section. Full implementation: OMN-7966.
    """

    def handle(self, command: object) -> dict[str, object]:
        """Execute the PR Review Bot FSM pipeline.

        Reads topics from contract.yaml at runtime — never hardcodes them.
        Full implementation wires sub-handlers from OMN-7966.
        """
        logger.info("HandlerPrReviewBot.handle invoked — delegating to FSM (OMN-7966)")
        msg = (
            "HandlerPrReviewBot.handle is not yet wired to sub-handlers. "
            "Call run_full_pipeline() from the effects layer with injected protocols."
        )
        raise NotImplementedError(msg)  # stub-ok


__all__: list[str] = ["HandlerPrReviewBot"]
