"""HandlerDodVerify — DoD evidence verification compute node.

Simple compute: load contract -> run evidence checks -> emit report.
Not a multi-phase FSM — single-shot computation.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

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

logger = logging.getLogger(__name__)


class HandlerDodVerify:
    """Handler for DoD evidence verification.

    Pure logic — no external I/O. Callers provide evidence check results.
    """

    def start(self, command: ModelDodVerifyStartCommand) -> ModelDodVerifyState:
        """Initialize verification state from a start command."""
        return ModelDodVerifyState(
            correlation_id=command.correlation_id,
            ticket_id=command.ticket_id,
            status=EnumDodVerifyStatus.PENDING,
            dry_run=command.dry_run,
        )

    def run_checks(
        self,
        state: ModelDodVerifyState,
        evidence_results: list[ModelEvidenceCheckResult],
    ) -> ModelDodVerifyState:
        """Run evidence checks and update state with results."""
        verified = sum(
            1 for r in evidence_results if r.status == EnumEvidenceCheckStatus.VERIFIED
        )
        failed = sum(
            1 for r in evidence_results if r.status == EnumEvidenceCheckStatus.FAILED
        )
        skipped = sum(
            1 for r in evidence_results if r.status == EnumEvidenceCheckStatus.SKIPPED
        )

        if failed > 0:
            overall = EnumDodVerifyStatus.FAILED
        elif len(evidence_results) == 0:
            overall = EnumDodVerifyStatus.SKIPPED
        else:
            overall = EnumDodVerifyStatus.VERIFIED

        return state.model_copy(
            update={
                "status": overall,
                "checks": evidence_results,
                "total_checks": len(evidence_results),
                "verified_count": verified,
                "failed_count": failed,
                "skipped_count": skipped,
            }
        )

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

    def handle(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """RuntimeLocal handler protocol shim.

        Delegates to run_verification with a ModelDodVerifyStartCommand
        constructed from input_data.
        """
        evidence_results_raw = input_data.pop("evidence_results", [])
        command = ModelDodVerifyStartCommand(**input_data)
        evidence_results = [ModelEvidenceCheckResult(**r) for r in evidence_results_raw]
        state, _completed = self.run_verification(command, evidence_results)
        return state.model_dump(mode="json")

    def run_verification(
        self,
        command: ModelDodVerifyStartCommand,
        evidence_results: list[ModelEvidenceCheckResult] | None = None,
    ) -> tuple[ModelDodVerifyState, ModelDodVerifyCompletedEvent]:
        """Run a complete verification with provided evidence results.

        Deterministic entry point for testing.
        """
        started_at = datetime.now(tz=UTC)
        state = self.start(command)
        state = self.run_checks(state, evidence_results or [])
        completed = self.make_completed_event(state, started_at)
        return state, completed


__all__: list[str] = ["HandlerDodVerify"]
