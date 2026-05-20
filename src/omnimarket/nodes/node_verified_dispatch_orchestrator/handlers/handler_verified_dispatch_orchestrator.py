# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Orchestrator handler for verified dispatch.

Coordinates paired worker+verifier subagent dispatch with bounded escalation.
The verifier independently queries authoritative surfaces and produces a
typed ModelVerificationBundle. Escalation fires on third consecutive rejection.

Related:
    - OMN-11220: Verification-First Parallel Worker Dispatch Skill
    - OMN-11219: (parent epic)
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from omnimarket.nodes.node_verified_dispatch_orchestrator.models.model_dispatch_request import (
    ModelDispatchRequest,
)
from omnimarket.nodes.node_verified_dispatch_orchestrator.models.model_escalation_policy import (
    ModelEscalationPolicy,
)
from omnimarket.nodes.node_verified_dispatch_orchestrator.models.model_verification_bundle import (
    ModelAuthoritativeCheck,
    ModelDetectedMismatch,
    ModelVerificationBundle,
)

logger = logging.getLogger(__name__)

# Authoritative surfaces the verifier queries for each worker run.
_AUTHORITATIVE_SURFACES: tuple[str, ...] = (
    "github_pr",
    "ci_checks",
    "linear_ticket",
    "projection_api",
    "topology_manifest",
    "deployment_receipt",
    "artifact_hash",
)


class HandlerVerifiedDispatchOrchestrator:
    """Orchestrates paired worker/verifier subagent dispatch with bounded escalation.

    This handler implements the verified dispatch loop:
    1. Dispatch worker subagent with the given prompt.
    2. Dispatch verifier subagent to independently query authoritative surfaces.
    3. If verifier rejects and attempts remain, wait cooldown and retry.
    4. On max_attempts exhaustion, escalate per the escalation_action policy.

    The handler is designed to be driven by the ONEX runtime via the contract-
    declared command topic. In non-runtime (test) contexts, call ``dispatch``
    directly.
    """

    def dispatch(self, request: ModelDispatchRequest) -> dict[str, Any]:
        """Run the verified dispatch loop and return the final outcome.

        Args:
            request: Dispatch configuration including ticket_id, worker_prompt,
                     and escalation policy parameters.

        Returns:
            A dict with keys: verification_bundle, decision, attempt_count, escalated.
        """
        policy = ModelEscalationPolicy(
            max_attempts=request.max_attempts,
            cooldown_seconds=request.cooldown_seconds,
            escalation_action=request.escalation_action,
        )
        correlation_id = request.correlation_id or str(uuid.uuid4())

        bundle: ModelVerificationBundle | None = None
        attempt = 0

        while attempt < policy.max_attempts:
            attempt += 1
            logger.info(
                "verified_dispatch attempt %d/%d ticket=%s correlation=%s",
                attempt,
                policy.max_attempts,
                request.ticket_id,
                correlation_id,
            )

            worker_run_id = str(uuid.uuid4())
            verifier_run_id = str(uuid.uuid4())

            worker_claim = self._run_worker(
                worker_run_id=worker_run_id,
                prompt=request.worker_prompt,
                ticket_id=request.ticket_id,
            )

            bundle = self._run_verifier(
                verifier_run_id=verifier_run_id,
                worker_run_id=worker_run_id,
                worker_claim=worker_claim,
                ticket_id=request.ticket_id,
                correlation_id=correlation_id,
            )

            if bundle.decision == "accept":
                logger.info(
                    "verified_dispatch accepted on attempt %d ticket=%s",
                    attempt,
                    request.ticket_id,
                )
                return {
                    "verification_bundle": bundle.model_dump(mode="json"),
                    "decision": "accept",
                    "attempt_count": attempt,
                    "escalated": False,
                }

            logger.warning(
                "verified_dispatch rejected on attempt %d ticket=%s mismatches=%d",
                attempt,
                request.ticket_id,
                len(bundle.detected_mismatches),
            )

            if attempt < policy.max_attempts:
                logger.info(
                    "verified_dispatch cooling down %ds before attempt %d",
                    policy.cooldown_seconds,
                    attempt + 1,
                )
                if policy.cooldown_seconds > 0:
                    time.sleep(policy.cooldown_seconds)

        # Max attempts exhausted — escalate.
        logger.error(
            "verified_dispatch exhausted %d attempts ticket=%s escalation=%s",
            policy.max_attempts,
            request.ticket_id,
            policy.escalation_action,
        )
        self._escalate(
            ticket_id=request.ticket_id,
            correlation_id=correlation_id,
            escalation_action=policy.escalation_action,
            bundle=bundle,
        )

        return {
            "verification_bundle": bundle.model_dump(mode="json") if bundle else None,
            "decision": "reject",
            "attempt_count": attempt,
            "escalated": True,
        }

    def _run_worker(
        self,
        *,
        worker_run_id: str,
        prompt: str,
        ticket_id: str,
    ) -> str:
        """Dispatch the worker subagent and return its top-level claim.

        In production this delegates to the ONEX agent dispatch surface.
        Returns a structured claim string the verifier can parse.
        """
        logger.debug(
            "worker_dispatch run_id=%s ticket=%s prompt_len=%d",
            worker_run_id,
            ticket_id,
            len(prompt),
        )
        # The claim encodes the worker run ID so the verifier can correlate evidence.
        return f"worker_run_id={worker_run_id} ticket={ticket_id} prompt_hash={hash(prompt)}"

    def _run_verifier(
        self,
        *,
        verifier_run_id: str,
        worker_run_id: str,
        worker_claim: str,
        ticket_id: str,
        correlation_id: str,
    ) -> ModelVerificationBundle:
        """Dispatch the verifier subagent and return the verification bundle.

        The verifier independently queries each authoritative surface.
        In production this drives node_overseer_verifier or equivalent.
        """
        logger.debug(
            "verifier_dispatch run_id=%s worker_run_id=%s ticket=%s",
            verifier_run_id,
            worker_run_id,
            ticket_id,
        )
        checks = self._query_authoritative_surfaces(
            worker_claim=worker_claim,
            ticket_id=ticket_id,
        )
        mismatches = [
            ModelDetectedMismatch(
                surface=c.surface,
                worker_claim=worker_claim,
                actual_state=c.result,
                severity="critical",
            )
            for c in checks
            if not c.passed
        ]
        decision: str = "accept" if not mismatches else "reject"
        return ModelVerificationBundle(
            worker_run_id=worker_run_id,
            verifier_run_id=verifier_run_id,
            claim=worker_claim,
            authoritative_checks=checks,
            detected_mismatches=mismatches,
            decision=decision,
            evidence_refs=[],
            timestamp_utc=datetime.now(UTC),
            correlation_id=correlation_id,
        )

    def _query_authoritative_surfaces(
        self,
        *,
        worker_claim: str,
        ticket_id: str,
    ) -> list[ModelAuthoritativeCheck]:
        """Query each authoritative surface declared in _AUTHORITATIVE_SURFACES.

        In production each surface check delegates to its respective probe node
        (node_pr_snapshot_effect, node_overseer_verifier, etc.). This method
        encodes the surface query contract; production injection overrides each
        surface via the DI container.
        """
        results: list[ModelAuthoritativeCheck] = []
        for surface in _AUTHORITATIVE_SURFACES:
            passed, result = self._probe_surface(
                surface=surface,
                worker_claim=worker_claim,
                ticket_id=ticket_id,
            )
            results.append(
                ModelAuthoritativeCheck(
                    surface=surface,
                    query=f"probe:{surface} ticket={ticket_id}",
                    result=result,
                    passed=passed,
                )
            )
        return results

    def _probe_surface(
        self,
        *,
        surface: str,
        worker_claim: str,
        ticket_id: str,
    ) -> tuple[bool, str]:
        """Probe a single authoritative surface.

        Subclasses or DI-injected probes override this per surface type.
        The base implementation always passes so the handler remains testable
        without external services. Production subclasses replace this with
        real surface queries.
        """
        return (True, f"surface={surface} not probed (stub); claim accepted")

    def _escalate(
        self,
        *,
        ticket_id: str,
        correlation_id: str,
        escalation_action: str,
        bundle: ModelVerificationBundle | None,
    ) -> None:
        """Execute the configured escalation action."""
        mismatch_summary = (
            "; ".join(
                f"{m.surface}: {m.actual_state}" for m in bundle.detected_mismatches
            )
            if bundle
            else "no bundle"
        )
        logger.error(
            "ESCALATION action=%s ticket=%s correlation=%s mismatches=[%s]",
            escalation_action,
            ticket_id,
            correlation_id,
            mismatch_summary,
        )
        if escalation_action == "linear_ticket":
            self._create_linear_escalation_ticket(
                ticket_id=ticket_id,
                correlation_id=correlation_id,
                mismatch_summary=mismatch_summary,
            )
        # "human_review" escalation is handled by the caller / event bus consumer.

    def _create_linear_escalation_ticket(
        self,
        *,
        ticket_id: str,
        correlation_id: str,
        mismatch_summary: str,
    ) -> None:
        """Create a Linear escalation ticket via the project tracker protocol.

        In production this resolves via the DI container's ProtocolProjectTracker.
        Logs the escalation so it is observable in CI without a live Linear connection.
        Production subclasses replace this with a real ProtocolProjectTracker call.
        """
        logger.error(
            "LINEAR_ESCALATION ticket=%s correlation=%s summary=%s",
            ticket_id,
            correlation_id,
            mismatch_summary,
        )


__all__: list[str] = ["HandlerVerifiedDispatchOrchestrator"]
