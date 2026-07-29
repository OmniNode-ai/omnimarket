"""HandlerDodVerify — DoD evidence verification compute node.

Simple compute: load contract -> run evidence checks -> emit report.
Not a multi-phase FSM — single-shot computation.

When callers provide pre-collected ``evidence_results``, the handler is pure
(no I/O). When ``evidence_results`` is None, the handler uses
EvidenceCollector to load the ticket contract and run checks — this is the
primary execution path for RuntimeLocal and onex run-node invocations.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from omnimarket.nodes.node_dod_verify.models.model_dod_verify_completed_event import (
    ModelDodVerifyCompletedEvent,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_start_command import (
    ModelDodVerifyStartCommand,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumDodVerifyStatus,
    EnumEvidenceCheckStatus,
    ModelDodVerifyState,
    ModelEvidenceCheckResult,
)

if TYPE_CHECKING:
    from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
        EvidenceCollector,
    )

logger = logging.getLogger(__name__)


class HandlerDodVerify:
    """Handler for DoD evidence verification.

    When ``evidence_results`` are provided, behaves as pure logic (no I/O).
    When ``evidence_results`` is None, loads the ticket contract and runs
    evidence checks via EvidenceCollector.
    """

    def handle(
        self,
        payload: ModelDodVerifyStartCommand | dict[str, object],
        *,
        evidence_results: list[ModelEvidenceCheckResult] | None = None,
    ) -> ModelDodVerifyState | dict[str, object]:
        """Run DoD evidence verification and return final state.

        Supports two calling conventions:
        - Typed: handle(ModelDodVerifyStartCommand, ...) -> ModelDodVerifyState
        - RuntimeLocal shim: handle(dict) -> dict  (required by RuntimeLocal contract)

        OMN-13253: the first parameter is named ``payload`` so the RuntimeLocal
        adapter's single-parameter dispatch passes the validated command/dict
        positionally instead of keyword-fanning the model fields, and
        ``evidence_results`` is keyword-only so the adapter sees exactly one
        positional parameter.
        """
        if isinstance(payload, dict):
            return self._handle_dict(payload)
        return self._handle_typed(payload, evidence_results)

    def _handle_dict(self, payload: dict[str, object]) -> dict[str, object]:
        """RuntimeLocal shim — translates dict in/out to typed handle."""
        command = ModelDodVerifyStartCommand(**payload)
        state = self._handle_typed(command)
        return state.model_dump(mode="json")

    @staticmethod
    def _make_collector() -> EvidenceCollector:
        """Create an EvidenceCollector instance. Override in tests to mock."""
        from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
            EvidenceCollector,
        )

        return EvidenceCollector()

    def _handle_typed(
        self,
        command: ModelDodVerifyStartCommand,
        evidence_results: list[ModelEvidenceCheckResult] | None = None,
    ) -> ModelDodVerifyState:
        """Run DoD evidence verification and return final state.

        Canonical typed entry point. Accepts a start command and optional
        pre-collected evidence results. When evidence_results is None,
        loads the contract and collects evidence automatically.
        """
        if evidence_results is None:
            collector = self._make_collector()
            evidence_results = collector.collect(
                ticket_id=command.ticket_id,
                contract_path=command.contract_path,
            )

        checks = evidence_results

        verified = sum(
            1 for r in checks if r.status == EnumEvidenceCheckStatus.VERIFIED
        )
        failed = sum(1 for r in checks if r.status == EnumEvidenceCheckStatus.FAILED)
        skipped = sum(1 for r in checks if r.status == EnumEvidenceCheckStatus.SKIPPED)

        if failed > 0:
            overall = EnumDodVerifyStatus.FAILED
        elif len(checks) == 0 or skipped == len(checks):
            # No checks ran, or every check that ran was skipped — nothing was
            # actually verified. Do not claim VERIFIED (OMN-15380: an empty
            # verification set is not a pass).
            overall = EnumDodVerifyStatus.SKIPPED
        else:
            overall = EnumDodVerifyStatus.VERIFIED

        error_message: str | None = None
        if overall == EnumDodVerifyStatus.SKIPPED:
            # OMN-15380: surface a distinct, machine-checkable reason so callers
            # that only read ``error_message`` (e.g. RuntimeLocal._classify_result,
            # which treats a populated error_message as an unambiguous failure
            # signal) fail closed instead of silently succeeding on zero verified
            # checks. Missing/unresolvable contract gets its own reason code
            # because it is the highest-risk case: a ticket with no DoD contract
            # at all is exactly the one most likely to be a false-Done.
            if (
                len(checks) == 1
                and checks[0].evidence_id == "contract"
                and checks[0].status == EnumEvidenceCheckStatus.SKIPPED
            ):
                error_message = (
                    f"CONTRACT_MISSING: no DoD contract found for "
                    f"{command.ticket_id}; zero checks were verified"
                )
            else:
                error_message = (
                    f"NO_CHECKS_VERIFIED: 0/{len(checks)} evidence checks "
                    f"verified for {command.ticket_id}"
                )

        state = ModelDodVerifyState(
            correlation_id=command.correlation_id,
            ticket_id=command.ticket_id,
            status=overall,
            dry_run=command.dry_run,
            checks=checks,
            total_checks=len(checks),
            verified_count=verified,
            failed_count=failed,
            skipped_count=skipped,
            error_message=error_message,
        )

        return state

    def run_verification(
        self,
        command: ModelDodVerifyStartCommand,
        evidence_results: list[ModelEvidenceCheckResult] | None = None,
    ) -> tuple[ModelDodVerifyState, ModelDodVerifyCompletedEvent]:
        """Run a complete verification and return state + completion event.

        Convenience wrapper used by tests and event-bus consumers that need
        the completed event alongside the state.
        """
        started_at = datetime.now(tz=UTC)
        state = self._handle_typed(command, evidence_results)
        completed = self.make_completed_event(state, started_at)
        return state, completed

    def make_completed_event(
        self,
        state: ModelDodVerifyState,
        started_at: datetime,
    ) -> ModelDodVerifyCompletedEvent:
        """Create a completion event from the final state."""
        return ModelDodVerifyCompletedEvent(
            correlation_id=state.correlation_id,
            ticket_id=state.ticket_id,
            status=state.status,
            started_at=started_at,
            completed_at=datetime.now(tz=UTC),
            checks=state.checks,
            total_checks=state.total_checks,
            verified_count=state.verified_count,
            failed_count=state.failed_count,
            skipped_count=state.skipped_count,
            error_message=state.error_message,
        )

    def serialize_completed(self, event: ModelDodVerifyCompletedEvent) -> bytes:
        """Serialize a completed event to bytes."""
        return json.dumps(event.model_dump(mode="json")).encode()


__all__: list[str] = ["HandlerDodVerify"]
