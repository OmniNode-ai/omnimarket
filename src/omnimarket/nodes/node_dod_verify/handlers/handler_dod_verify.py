"""HandlerDodVerify — DoD evidence verification compute node.

Simple compute: load contract -> run evidence checks -> emit report.
Not a multi-phase FSM — single-shot computation.

When callers provide pre-collected ``evidence_results``, the handler is pure
(no I/O). When ``evidence_results`` is None, the handler uses
EvidenceCollector to load the ticket contract and run checks — this is the
primary execution path for RuntimeLocal and onex run-node invocations.

``run_and_persist()`` is the extension point for the ``--output-path`` CLI flag:
it calls the pure ``_handle_typed`` then hands the result to ``ReceiptWriter``.
``_handle_typed`` itself remains side-effect-free.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from omnimarket.nodes.node_dod_verify.models.model_dod_report_receipt import (
    ModelDodReportReceipt,
    ModelDodReportResult,
)
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
from omnimarket.nodes.node_dod_verify.receipt_writer import ReceiptWriter

if TYPE_CHECKING:
    from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
        EvidenceCollector,
    )

logger = logging.getLogger(__name__)

# Semver of this node — bumped from 1.0.0 to 1.1.0 when ReceiptWriter shipped.
# The hook's version_too_old check uses this to reject receipts from older builds.
_GENERATOR_VERSION = "1.1.0"


class HandlerDodVerify:
    """Handler for DoD evidence verification.

    When ``evidence_results`` are provided, behaves as pure logic (no I/O).
    When ``evidence_results`` is None, loads the ticket contract and runs
    evidence checks via EvidenceCollector.
    """

    def handle(
        self,
        command: ModelDodVerifyStartCommand | dict[str, object],
        evidence_results: list[ModelEvidenceCheckResult] | None = None,
    ) -> ModelDodVerifyState | dict[str, object]:
        """Run DoD evidence verification and return final state.

        Supports two calling conventions:
        - Typed: handle(ModelDodVerifyStartCommand, ...) -> ModelDodVerifyState
        - RuntimeLocal shim: handle(dict) -> dict  (required by RuntimeLocal contract)
        """
        if isinstance(command, dict):
            return self._handle_dict(command)
        return self._handle_typed(command, evidence_results)

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
            overall = EnumDodVerifyStatus.SKIPPED
        elif skipped == len(checks):
            # All checks were skipped — do not claim VERIFIED
            overall = EnumDodVerifyStatus.SKIPPED
        else:
            overall = EnumDodVerifyStatus.VERIFIED

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
        )

        return state

    def run_verification(
        self,
        command: ModelDodVerifyStartCommand,
        evidence_results: list[ModelEvidenceCheckResult] | None = None,
    ) -> tuple[ModelDodVerifyState, ModelDodVerifyCompletedEvent]:
        """Run a complete verification and return state + completion event.

        Convenience wrapper used by tests and event-bus consumers that need
        the completed event alongside the state.  Does NOT write any files —
        use ``run_and_persist()`` when disk persistence is needed.
        """
        started_at = datetime.now(tz=UTC)
        state = self._handle_typed(command, evidence_results)
        completed = self.make_completed_event(state, started_at)
        return state, completed

    def run_and_persist(
        self,
        command: ModelDodVerifyStartCommand,
        writer: ReceiptWriter,
        output_path: Path | None = None,
        evidence_results: list[ModelEvidenceCheckResult] | None = None,
    ) -> tuple[ModelDodVerifyState, ModelDodVerifyCompletedEvent, Path]:
        """Run verification, write receipt to disk, return state + event + written path.

        This is the side-effecting entry point used by ``__main__.py`` when
        ``--output-path`` is set.  ``_handle_typed`` remains pure; all I/O is
        contained here.

        Args:
            command: The start command (parsed from CLI args or event payload).
            writer: Pre-configured ReceiptWriter (caller owns env/config loading).
            output_path: Explicit destination.  If None, ``writer`` resolves the
                canonical path from ``command.ticket_id``.
            evidence_results: Optional pre-collected results (tests / mocks).

        Returns:
            ``(state, completed_event, written_path)`` — the written_path is also
            set as ``completed_event.receipt_path``.
        """
        started_at = datetime.now(tz=UTC)
        state = self._handle_typed(command, evidence_results)
        receipt = self.build_receipt(state, command)
        written_path = writer.write(receipt, output_path)
        completed = self.make_completed_event(
            state, started_at, receipt_path=written_path
        )
        return state, completed, written_path

    def build_receipt(
        self,
        state: ModelDodVerifyState,
        command: ModelDodVerifyStartCommand,
    ) -> ModelDodReportReceipt:
        """Build a ModelDodReportReceipt from a completed state + originating command."""
        result = ModelDodReportResult(
            total=state.total_checks,
            verified=state.verified_count,
            failed=state.failed_count,
            skipped=state.skipped_count,
            status=state.status,
        )
        return ModelDodReportReceipt(
            timestamp=datetime.now(tz=UTC),
            ticket_id=state.ticket_id,
            generator_version=_GENERATOR_VERSION,
            node_correlation_id=state.correlation_id,
            contract_path=command.contract_path,
            result=result,
            checks=list(state.checks),
        )

    def make_completed_event(
        self,
        state: ModelDodVerifyState,
        started_at: datetime,
        receipt_path: Path | None = None,
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
            receipt_path=receipt_path,
        )

    def serialize_completed(self, event: ModelDodVerifyCompletedEvent) -> bytes:
        """Serialize a completed event to bytes."""
        return json.dumps(event.model_dump(mode="json")).encode()


__all__: list[str] = ["HandlerDodVerify"]
