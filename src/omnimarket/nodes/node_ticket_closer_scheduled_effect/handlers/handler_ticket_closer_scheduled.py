# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ticket closer on the runtime's schedule (OMN-24138).

handle takes the runtime tick. A tick inside the overlay's interval returns nothing and does not move the
last-run time; the tick that is due reads the candidate tickets through the source the overlay names, runs the
DoD verifier against the overlay's execution audience, decides each candidate with the flip predicate the
closeout sweep already owns (including its runtime-proof conjunct) and moves the ones that pass to the
overlay's done state through the ticket write node. A verifier that cannot run judges nothing: its ticket is
reported and nothing is written. Nothing here names a cadence, a path or a host.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayNotFoundError,
    load_structured_overlay,
)
from omnimarket.handlers.scheduled_overlay_refusal import ScheduledOverlayRefusalLatch
from omnimarket.models.runtime_proof.model_runtime_proof_close import (
    EnumRuntimeReceiptKind,
    ModelProofCloseRequest,
    ModelRuntimeReceipt,
)
from omnimarket.models.ticket_closer import (
    EnumCloserAction,
    ModelCloserCandidate,
    ModelCloserTicketOutcome,
    ModelCloserVerifyReading,
    ModelCloserWriteReceipt,
    ModelTicketCloserResult,
    ModelTicketCloserScheduleOverlay,
)
from omnimarket.nodes.node_dod_closeout_sweep_compute.handlers.handler_dod_closeout_sweep import (
    flip_decision,
)
from omnimarket.nodes.node_dod_closeout_sweep_compute.models.model_dod_closeout_sweep import (
    EnumCloseoutAction,
    EnumCloseoutDecisionKind,
    ModelDodCloseoutDecisionRequest,
)

SCHEDULE_OVERLAY_ID = "node_ticket_closer_scheduled_effect"
SCHEDULE_OVERLAY_FILE_ENV = "ONEX_TICKET_CLOSER_SCHEDULE_OVERLAY"


@runtime_checkable
class ProtocolCloserCandidateSource(Protocol):
    """One read of the closer's candidate tickets for one run.

    ``None`` is the source that could not read: the reading is unread, never an empty success.
    """

    def candidates(
        self, now: datetime, limit: int
    ) -> list[ModelCloserCandidate] | None: ...


@runtime_checkable
class ProtocolCloserVerifier(Protocol):
    """One run of the DoD verifier for one ticket against one execution audience."""

    def verify(
        self, ticket_id: str, execution_audience: str
    ) -> ModelCloserVerifyReading: ...


@runtime_checkable
class ProtocolTicketDoneWriter(Protocol):
    """One move of a ticket to the done state through the ticket write node."""

    def mark_done(
        self, ticket_id: str, *, state: str, lane: str, evidence: str
    ) -> ModelCloserWriteReceipt: ...


class UnreadCloserCandidates:
    """The in-memory default profile: the source could not read, so no candidate is drawn."""

    def candidates(
        self, now: datetime, limit: int
    ) -> list[ModelCloserCandidate] | None:
        return None


class UnboundCloserVerifier:
    """The in-memory default verifier: nothing is bound, so a verify cannot run."""

    def verify(
        self, ticket_id: str, execution_audience: str
    ) -> ModelCloserVerifyReading:
        return ModelCloserVerifyReading(
            ticket_id=ticket_id,
            ran=False,
            error_code="VERIFIER_NOT_BOUND",
        )


class RefusingDoneWriter:
    """The in-memory default writer: nothing is bound, so a move to done refuses."""

    def mark_done(
        self, ticket_id: str, *, state: str, lane: str, evidence: str
    ) -> ModelCloserWriteReceipt:
        return ModelCloserWriteReceipt(ok=False, reason="writer not bound")


class HandlerTicketCloserScheduled:
    """Gate on the schedule, examine the candidates, flip the ones the predicate passes, return the result."""

    def __init__(
        self,
        *,
        schedule: ModelTicketCloserScheduleOverlay | None = None,
        source: ProtocolCloserCandidateSource | None = None,
        verifier: ProtocolCloserVerifier | None = None,
        writer: ProtocolTicketDoneWriter | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._schedule = schedule
        self._source: ProtocolCloserCandidateSource = source or UnreadCloserCandidates()
        self._verifier: ProtocolCloserVerifier = verifier or UnboundCloserVerifier()
        self._writer: ProtocolTicketDoneWriter = writer or RefusingDoneWriter()
        self._environ = environ
        self._last_run: datetime | None = None
        self._refusals = ScheduledOverlayRefusalLatch(SCHEDULE_OVERLAY_ID)

    def _env(self) -> Mapping[str, str]:
        return os.environ if self._environ is None else self._environ

    def _schedule_overlay(self) -> ModelTicketCloserScheduleOverlay:
        if self._schedule is None:
            self._schedule = load_structured_overlay(
                SCHEDULE_OVERLAY_ID,
                ModelTicketCloserScheduleOverlay,
                self._env(),
                file_env=SCHEDULE_OVERLAY_FILE_ENV,
            )
        return self._schedule

    def _runtime_proof(
        self, candidate: ModelCloserCandidate, reading: ModelCloserVerifyReading
    ) -> ModelProofCloseRequest | None:
        # A clean passing verifier is itself one runtime receipt for the proof's conjunct; a
        # verifier that did not run, failed a check or read no revision adds nothing.
        proof = candidate.runtime_proof
        if (
            proof is not None
            and reading.error_code == ""
            and reading.failed_count == 0
            and reading.behavior_proving_count > 0
            and reading.head_revision
        ):
            receipt = ModelRuntimeReceipt(
                kind=EnumRuntimeReceiptKind.DOD_VERIFY_PASS,
                passed=True,
                revision=reading.head_revision,
                exercises_running_system=reading.exercises_running_system,
            )
            proof = proof.model_copy(update={"receipts": (*proof.receipts, receipt)})
        return proof

    def handle(self, request: ModelRuntimeTick) -> ModelTicketCloserResult | None:
        try:
            schedule = self._schedule_overlay()
        except NodeOverlayNotFoundError as exc:
            self._refusals.refuse(exc)
            return None
        now = request.now.astimezone(UTC)
        last = self._last_run
        if last is not None and (now - last) < timedelta(
            seconds=schedule.run_interval_seconds
        ):
            return None
        candidates = self._source.candidates(now, schedule.max_candidates_per_run)
        self._last_run = now
        self._refusals.recovered()
        if candidates is None:
            return ModelTicketCloserResult(
                tick_id=str(request.tick_id),
                computed_at=now,
                candidates_read=False,
                examined=0,
                flipped=0,
                outcomes=(),
            )
        outcomes: list[ModelCloserTicketOutcome] = []
        for candidate in candidates[: schedule.max_candidates_per_run]:
            try:
                reading = self._verifier.verify(
                    candidate.ticket_id, schedule.execution_audience
                )
            except Exception as exc:
                outcomes.append(
                    ModelCloserTicketOutcome(
                        ticket_id=candidate.ticket_id,
                        action=EnumCloserAction.VERIFY_NOT_RUN,
                        reason=f"verifier raised {type(exc).__name__}",
                    )
                )
                continue
            if not reading.ran:
                outcomes.append(
                    ModelCloserTicketOutcome(
                        ticket_id=candidate.ticket_id,
                        action=EnumCloserAction.VERIFY_NOT_RUN,
                        reason=f"{reading.error_code}: {reading.error_detail}".strip(),
                    )
                )
                continue
            proof = self._runtime_proof(candidate, reading)
            decision = flip_decision(
                request=ModelDodCloseoutDecisionRequest(
                    kind=EnumCloseoutDecisionKind.FLIP_DECISION,
                    date=now.date().isoformat(),
                    ticket_id=candidate.ticket_id,
                    total_checks=reading.total_checks,
                    verified_count=reading.verified_count,
                    failed_count=reading.failed_count,
                    non_probative_count=reading.non_probative_count,
                    behavior_proving_count=reading.behavior_proving_count,
                    criteria=list(candidate.criteria),
                    bound_criteria=list(reading.bound_criteria),
                    prior_reversal=candidate.prior_reversal,
                    outcome_changed_since_reversal=candidate.outcome_changed_since_reversal,
                    all_prs_merged=candidate.all_prs_merged,
                    runtime_proof=proof,
                )
            )
            if decision.action is EnumCloseoutAction.FLIPPED_DONE:
                try:
                    receipt = self._writer.mark_done(
                        candidate.ticket_id,
                        state=schedule.done_state,
                        lane=schedule.lane,
                        evidence=(
                            f"dod_verify total={reading.total_checks} "
                            f"verified={reading.verified_count} "
                            f"behavior_proving={reading.behavior_proving_count} "
                            f"revision={reading.head_revision}"
                        ),
                    )
                except Exception as exc:
                    outcomes.append(
                        ModelCloserTicketOutcome(
                            ticket_id=candidate.ticket_id,
                            action=EnumCloserAction.WRITE_REFUSED,
                            reason=f"writer raised {type(exc).__name__}",
                        )
                    )
                    continue
                if receipt.ok:
                    outcomes.append(
                        ModelCloserTicketOutcome(
                            ticket_id=candidate.ticket_id,
                            action=EnumCloserAction.FLIPPED_DONE,
                        )
                    )
                else:
                    outcomes.append(
                        ModelCloserTicketOutcome(
                            ticket_id=candidate.ticket_id,
                            action=EnumCloserAction.WRITE_REFUSED,
                            reason=receipt.reason,
                        )
                    )
            else:
                outcomes.append(
                    ModelCloserTicketOutcome(
                        ticket_id=candidate.ticket_id,
                        action=EnumCloserAction.HELD,
                        reason=decision.reason or decision.primary_blocker or "",
                        unmet=tuple(decision.unmet or ()),
                    )
                )
        return ModelTicketCloserResult(
            tick_id=str(request.tick_id),
            computed_at=now,
            candidates_read=True,
            examined=len(outcomes),
            flipped=sum(
                1
                for outcome in outcomes
                if outcome.action is EnumCloserAction.FLIPPED_DONE
            ),
            outcomes=tuple(outcomes),
        )
