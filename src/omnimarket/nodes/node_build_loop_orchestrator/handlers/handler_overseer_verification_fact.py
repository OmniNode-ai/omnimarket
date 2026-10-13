"""HandlerOverseerVerificationFact -- acknowledges the verifier-completed route.

The orchestrator contract subscribes to the overseer verifier's completed fact
so a VERIFYING phase can await it. That wait is an in-process bus subscription
inside ``HandlerBuildLoopOrchestrator``; the contract's handler route for the
same topic only has to consume the fact without starting a cycle and without
adding a result to the build-loop completed topic, so it acknowledges and
returns nothing.
"""

from __future__ import annotations

import logging

from omnimarket.nodes.node_build_loop_orchestrator.models.model_overseer_verification_fact import (
    ModelOverseerVerificationFact,
)

logger = logging.getLogger(__name__)


class HandlerOverseerVerificationFact:
    """Consume a verifier-completed fact; the VERIFYING wait owns acting on it."""

    def handle(self, request: ModelOverseerVerificationFact) -> None:
        logger.debug(
            "[BUILD-LOOP-ORCH] Overseer verification fact consumed "
            "(correlation_id=%s, passed=%s)",
            request.correlation_id,
            request.passed,
        )


__all__: list[str] = ["HandlerOverseerVerificationFact"]
