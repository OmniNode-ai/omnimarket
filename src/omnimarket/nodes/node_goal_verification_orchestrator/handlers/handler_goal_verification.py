# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Closing a goal starts its verification: one run per goal id and contract revision (OMN-20033).

The protocol is the one the design-reviewed GC.10 closure model checks:

* the run is keyed on the goal id and the revision; closing event ids are provenance (F1);
* a close is recorded PENDING before anything is published, the start command is published
  durably, and only then is the attempt recorded; the event is acknowledged after both (F2, F7);
  the verifier accepts a start command once per key, so a redelivery between the two writes
  republishes the same command and starts no second run;
* a disposition is recorded and never publishes a start command (F4, R4);
* a run whose verifier goes quiet is retried up to the contract's bound, then written as an
  obligation row, and a late verdict clears the obligation (F5, R3);
* every close without a verdict is listed from the moment it is delivered (F7, R5).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid5

import yaml
from omnibase_core.models.events.work.model_work_result_recorded import (
    ModelWorkResultRecorded,
)

from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_terminal_v2 import (
    ModelDelegationTerminalCompletedV2,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_completed_event import (
    ModelDodVerifyCompletedEvent,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_start_command import (
    ModelDodVerifyStartCommand,
)
from omnimarket.nodes.node_goal_verification_orchestrator.handlers.handler_goal_verification_store_inmemory import (
    StoreGoalVerificationInMemory,
)
from omnimarket.nodes.node_goal_verification_orchestrator.models.model_goal_verification import (
    EnumGoalClosureSource,
    EnumGoalVerificationAction,
    EnumGoalVerificationState,
    ModelGoalClosure,
    ModelGoalDispositionRecorded,
    ModelGoalVerificationDecision,
    ModelGoalVerificationOutcome,
    ModelGoalVerificationRules,
    ModelGoalVerificationSweepCommand,
    ModelUnverifiedClosesList,
    ModelUnverifiedClosesQuery,
)
from omnimarket.nodes.node_goal_verification_orchestrator.protocols.protocol_goal_verification import (
    ProtocolGoalContractReader,
    ProtocolGoalVerificationStore,
    ProtocolVerifierStartPublisher,
)

CONTRACT_PATH = Path(__file__).parents[1] / "contract.yaml"


def load_rules(contract: Path = CONTRACT_PATH) -> ModelGoalVerificationRules:
    """Read the verification_rules of a contract; a missing or unknown rule is refused."""
    data = yaml.safe_load(contract.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(
        data.get("verification_rules"), dict
    ):
        raise ValueError("contract verification_rules must be a mapping")
    return ModelGoalVerificationRules.model_validate(data["verification_rules"])


class _RefusingPublisher:
    """The default publisher: a node wired without one cannot silently drop a close."""

    def publish(self, command: ModelDodVerifyStartCommand) -> None:
        raise RuntimeError(
            "no verifier start publisher is wired; the close stays pending"
        )


class _EmptyContractReader:
    def read(self, goal_id: UUID, contract_revision: UUID) -> None:
        return None

    def find_by_delegation_correlation(self, delegation_correlation_id: UUID) -> None:
        return None


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


class GoalVerificationEngine:
    """The orchestrator's state machine over injected store, contract reader and publisher."""

    def __init__(
        self,
        *,
        rules: ModelGoalVerificationRules | None = None,
        store: ProtocolGoalVerificationStore | None = None,
        contract_reader: ProtocolGoalContractReader | None = None,
        publisher: ProtocolVerifierStartPublisher | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.rules = rules if rules is not None else load_rules()
        self.store: ProtocolGoalVerificationStore = (
            store if store is not None else StoreGoalVerificationInMemory()
        )
        self._reader: ProtocolGoalContractReader = (
            contract_reader if contract_reader is not None else _EmptyContractReader()
        )
        self._publisher: ProtocolVerifierStartPublisher = (
            publisher if publisher is not None else _RefusingPublisher()
        )
        self._clock = clock

    def now(self) -> datetime:
        return self._clock()

    @property
    def contract_reader(self) -> ProtocolGoalContractReader:
        return self._reader

    def run_correlation_id(self, goal_id: UUID, contract_revision: UUID) -> UUID:
        """The verifier's run key as a correlation id: the same for every start of this run."""
        return uuid5(self.rules.run_key_namespace, f"{goal_id}:{contract_revision}")

    def close(self, closure: ModelGoalClosure) -> ModelGoalVerificationDecision:
        run, created = self.store.record_close(closure)
        if run.state is not EnumGoalVerificationState.PENDING:
            return self.decision(
                EnumGoalVerificationAction.DUPLICATE_CLOSE,
                run.goal_id,
                run.contract_revision,
                run.state,
                run.attempts,
                f"closed already; closing event {closure.closing_event_id} kept as provenance",
            )
        return self._start(run.goal_id, run.contract_revision, created)

    def _start(
        self, goal_id: UUID, contract_revision: UUID, created: bool
    ) -> ModelGoalVerificationDecision:
        contract = self._reader.read(goal_id, contract_revision)
        if contract is None:
            run = self.store.mark_unread_attempt(
                goal_id, contract_revision, self._clock()
            )
            return self.decision(
                EnumGoalVerificationAction.CONTRACT_UNREAD,
                goal_id,
                contract_revision,
                run.state,
                run.attempts,
                "the goal contract at this revision could not be read; the close stays listed",
            )
        existing = self.store.get_run(goal_id, contract_revision)
        command = ModelDodVerifyStartCommand(
            ticket_id=contract.ticket_id,
            correlation_id=self.run_correlation_id(goal_id, contract_revision),
            delegation_correlation_id=(
                existing.delegation_correlation_id if existing else None
            ),
            goal_id=goal_id,
            level=contract.level,
            contract_revision=contract_revision,
            contract_schema_version=contract.contract_schema_version,
            dod_evidence=contract.dod_evidence,
            requested_at=self._clock(),
        )
        # Publish first, record second: a crash between them republishes the same command.
        self._publisher.publish(command)
        recorded = self.store.mark_attempt(goal_id, contract_revision, self._clock())
        return self.decision(
            EnumGoalVerificationAction.STARTED,
            goal_id,
            contract_revision,
            recorded.state,
            recorded.attempts,
            "start command published" if created else "start command republished",
        )

    def disposition(
        self, event: ModelGoalDispositionRecorded
    ) -> ModelGoalVerificationDecision:
        """Record a disposition. It publishes nothing and creates no run."""
        self.store.record_disposition(
            event.goal_id, event.contract_revision, event.event_id
        )
        run = self.store.get_run(event.goal_id, event.contract_revision)
        return self.decision(
            EnumGoalVerificationAction.DISPOSITION_RECORDED,
            event.goal_id,
            event.contract_revision,
            run.state if run else None,
            run.attempts if run else 0,
            f"disposition {event.disposition!r} recorded",
        )

    def verdict(
        self, event: ModelDodVerifyCompletedEvent
    ) -> ModelGoalVerificationDecision:
        if event.goal_id is None or event.contract_revision is None:
            return self.decision(
                EnumGoalVerificationAction.VERDICT_UNMATCHED,
                event.goal_id,
                event.contract_revision,
                None,
                0,
                "the completion event names no goal and revision",
            )
        run = self.store.get_run(event.goal_id, event.contract_revision)
        if run is None:
            return self.decision(
                EnumGoalVerificationAction.VERDICT_UNMATCHED,
                event.goal_id,
                event.contract_revision,
                None,
                0,
                "no delivered close for this goal and revision",
            )
        bound = self.store.mark_verdict(
            event.goal_id, event.contract_revision, event.correlation_id
        )
        return self.decision(
            EnumGoalVerificationAction.VERDICT_BOUND,
            bound.goal_id,
            bound.contract_revision,
            bound.state,
            bound.attempts,
            f"verifier run {event.correlation_id} bound",
        )

    def sweep(self, now: datetime) -> tuple[ModelGoalVerificationDecision, ...]:
        """Retry runs whose verifier went quiet; write an obligation once the bound is spent."""
        decisions: list[ModelGoalVerificationDecision] = []
        for run in self.store.list_unverified():
            if run.state is EnumGoalVerificationState.EXHAUSTED:
                continue
            quiet_since = run.last_attempt_at or run.first_closed_at
            if (now - quiet_since).total_seconds() < self.rules.attempt_timeout_s:
                decisions.append(
                    self.decision(
                        EnumGoalVerificationAction.WAITING,
                        run.goal_id,
                        run.contract_revision,
                        run.state,
                        run.attempts,
                        "inside the attempt timeout",
                    )
                )
                continue
            if run.attempts >= self.rules.retry_bound:
                reason = (
                    "contract_unread"
                    if run.state is EnumGoalVerificationState.PENDING
                    else "verifier_attempts_exhausted"
                )
                exhausted = self.store.mark_exhausted(
                    run.goal_id, run.contract_revision, reason
                )
                decisions.append(
                    self.decision(
                        EnumGoalVerificationAction.EXHAUSTED,
                        exhausted.goal_id,
                        exhausted.contract_revision,
                        exhausted.state,
                        exhausted.attempts,
                        f"missing-verdict obligation: {reason}",
                    )
                )
                continue
            retried = self._start(run.goal_id, run.contract_revision, created=False)
            if retried.action is EnumGoalVerificationAction.STARTED:
                retried = retried.model_copy(
                    update={"action": EnumGoalVerificationAction.RETRIED}
                )
            decisions.append(retried)
        return tuple(decisions)

    def unverified(self, goal_id: UUID | None = None) -> ModelUnverifiedClosesList:
        runs = self.store.list_unverified()
        if goal_id is not None:
            runs = tuple(run for run in runs if run.goal_id == goal_id)
        return ModelUnverifiedClosesList(runs=runs)

    @staticmethod
    def decision(
        action: EnumGoalVerificationAction,
        goal_id: UUID | None,
        contract_revision: UUID | None,
        state: EnumGoalVerificationState | None,
        attempts: int,
        detail: str,
    ) -> ModelGoalVerificationDecision:
        return ModelGoalVerificationDecision(
            goal_id=goal_id,
            contract_revision=contract_revision,
            action=action,
            state=state,
            attempts=attempts,
            detail=detail,
        )


class HandlerGoalClosureResult:
    """A result event naming goals in closes_claims starts one run per goal and revision."""

    def __init__(self, engine: GoalVerificationEngine | None = None) -> None:
        self.engine = engine if engine is not None else GoalVerificationEngine()

    def handle(self, request: ModelWorkResultRecorded) -> ModelGoalVerificationOutcome:
        if request.contract_revision is None or not request.closes_claims:
            return ModelGoalVerificationOutcome(
                decisions=(
                    self.engine.decision(
                        EnumGoalVerificationAction.NO_GOAL,
                        None,
                        request.contract_revision,
                        None,
                        0,
                        "the result closes no goal or names no contract revision",
                    ),
                )
            )
        return ModelGoalVerificationOutcome(
            decisions=tuple(
                self.engine.close(
                    ModelGoalClosure(
                        goal_id=goal_id,
                        contract_revision=request.contract_revision,
                        closing_event_id=request.event_id,
                        source=EnumGoalClosureSource.RESULT,
                        observed_at=self.engine.now(),
                    )
                )
                for goal_id in sorted(request.closes_claims, key=str)
            )
        )


class HandlerGoalClosureDelegationTerminal:
    """A delegation terminal for a call goal starts its run."""

    def __init__(self, engine: GoalVerificationEngine | None = None) -> None:
        self.engine = engine if engine is not None else GoalVerificationEngine()

    def handle(
        self, request: ModelDelegationTerminalCompletedV2
    ) -> ModelGoalVerificationOutcome:
        goal = self.engine.contract_reader.find_by_delegation_correlation(
            request.correlation_id
        )
        if goal is None:
            return ModelGoalVerificationOutcome(
                decisions=(
                    self.engine.decision(
                        EnumGoalVerificationAction.NO_GOAL,
                        None,
                        None,
                        None,
                        0,
                        f"delegation run {request.correlation_id} belongs to no goal",
                    ),
                )
            )
        return ModelGoalVerificationOutcome(
            decisions=(
                self.engine.close(
                    ModelGoalClosure(
                        goal_id=goal.goal_id,
                        contract_revision=goal.revision_id,
                        closing_event_id=request.correlation_id,
                        source=EnumGoalClosureSource.DELEGATION_TERMINAL,
                        observed_at=self.engine.now(),
                    )
                ),
            )
        )


class HandlerGoalDisposition:
    """A disposition is recorded and starts nothing."""

    def __init__(self, engine: GoalVerificationEngine | None = None) -> None:
        self.engine = engine if engine is not None else GoalVerificationEngine()

    def handle(
        self, request: ModelGoalDispositionRecorded
    ) -> ModelGoalVerificationOutcome:
        return ModelGoalVerificationOutcome(
            decisions=(self.engine.disposition(request),)
        )


class HandlerGoalVerdictObserved:
    """A verifier completion event binds the verdict to the run."""

    def __init__(self, engine: GoalVerificationEngine | None = None) -> None:
        self.engine = engine if engine is not None else GoalVerificationEngine()

    def handle(
        self, request: ModelDodVerifyCompletedEvent
    ) -> ModelGoalVerificationOutcome:
        return ModelGoalVerificationOutcome(decisions=(self.engine.verdict(request),))


class HandlerGoalVerificationSweep:
    """Retry quiet runs up to the bound, then write the obligation."""

    def __init__(self, engine: GoalVerificationEngine | None = None) -> None:
        self.engine = engine if engine is not None else GoalVerificationEngine()

    def handle(
        self, request: ModelGoalVerificationSweepCommand
    ) -> ModelGoalVerificationOutcome:
        return ModelGoalVerificationOutcome(decisions=self.engine.sweep(request.now))


class HandlerUnverifiedCloses:
    """List every delivered close that has no verdict."""

    def __init__(self, engine: GoalVerificationEngine | None = None) -> None:
        self.engine = engine if engine is not None else GoalVerificationEngine()

    def handle(self, request: ModelUnverifiedClosesQuery) -> ModelUnverifiedClosesList:
        return self.engine.unverified(request.goal_id)


__all__: list[str] = [
    "CONTRACT_PATH",
    "GoalVerificationEngine",
    "HandlerGoalClosureDelegationTerminal",
    "HandlerGoalClosureResult",
    "HandlerGoalDisposition",
    "HandlerGoalVerdictObserved",
    "HandlerGoalVerificationSweep",
    "HandlerUnverifiedCloses",
    "load_rules",
]
