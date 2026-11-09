# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fixtures shared by the goal closure orchestrator tests (OMN-20033)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from omnibase_core.enums.enum_work_outcome import EnumWorkOutcome
from omnibase_core.enums.ticket.enum_dod_check_type import EnumDodCheckType
from omnibase_core.models.contracts.ticket.model_dod_evidence_check import (
    ModelDodEvidenceCheck,
)
from omnibase_core.models.events.work import ModelSessionActor
from omnibase_core.models.events.work.model_work_result_recorded import (
    ModelWorkResultRecorded,
)
from omnibase_core.models.ticket.model_contract_dod_item import ModelContractDodItem

from omnimarket.nodes.node_dod_verify.models.model_dod_verify_start_command import (
    ModelDodVerifyStartCommand,
)
from omnimarket.nodes.node_goal_verification_orchestrator.handlers.handler_goal_verification import (
    GoalVerificationEngine,
    load_rules,
)
from omnimarket.nodes.node_goal_verification_orchestrator.handlers.handler_goal_verification_store_inmemory import (
    StoreGoalVerificationInMemory,
)
from omnimarket.nodes.node_goal_verification_orchestrator.models.model_goal_verification import (
    ModelGoalContractRevision,
)

T0 = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
GOAL = UUID("747a3ba5-2ae9-4fa3-8c98-4d7475ce30c5")
REV1 = UUID("00000000-0000-4000-8000-000000000001")
REV2 = UUID("00000000-0000-4000-8000-000000000002")
DELEGATION_RUN = UUID("00000000-0000-4000-8000-0000000000d1")


def event_id(number: int) -> UUID:
    return UUID(f"00000000-0000-4000-8000-{number:012d}")


def goal_contract(
    revision: UUID = REV1, goal: UUID = GOAL
) -> ModelGoalContractRevision:
    return ModelGoalContractRevision(
        goal_id=goal,
        revision_id=revision,
        ticket_id="OMN-20033",
        level="workflow_lane",
        contract_schema_version="1.0.0",
        dod_evidence=(
            ModelContractDodItem(
                id="goal-disposition",
                description="Calling lane records its disposition",
                checks=[
                    ModelDodEvidenceCheck(
                        check_type=EnumDodCheckType.DISPOSITION,
                        check_value="lane-verdict",
                    )
                ],
            ),
        ),
    )


def result_event(
    number: int, goal: UUID = GOAL, revision: UUID | None = REV1
) -> ModelWorkResultRecorded:
    return ModelWorkResultRecorded(
        event_id=event_id(number),
        emitted_at=T0,
        actor=ModelSessionActor(session_handle="lane", agent_kind="build-lane"),
        summary="closed the goal",
        outcome=EnumWorkOutcome.TERMINAL,
        closes_claims=frozenset({goal}),
        contract_revision=revision,
        friction_none=True,
    )


class FakeClock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> datetime:
        self.now = self.now + timedelta(seconds=seconds)
        return self.now


class RecordingPublisher:
    """Stands for the durable bus publish; accepts a command once per verifier run key."""

    def __init__(self) -> None:
        self.published: list[ModelDodVerifyStartCommand] = []
        self.fail_next = False

    def publish(self, command: ModelDodVerifyStartCommand) -> None:
        if self.fail_next:
            self.fail_next = False
            raise ConnectionError("bus unavailable")
        self.published.append(command)

    @property
    def runs_started(self) -> int:
        """Distinct verifier runs: the verifier accepts a start command once per correlation id."""
        return len({command.correlation_id for command in self.published})


class DictContractReader:
    def __init__(self) -> None:
        self.contracts: dict[tuple[UUID, UUID], ModelGoalContractRevision] = {}
        self.by_delegation: dict[UUID, ModelGoalContractRevision] = {}

    def add(self, contract: ModelGoalContractRevision) -> None:
        self.contracts[(contract.goal_id, contract.revision_id)] = contract

    def read(
        self, goal_id: UUID, contract_revision: UUID
    ) -> ModelGoalContractRevision | None:
        return self.contracts.get((goal_id, contract_revision))

    def find_by_delegation_correlation(
        self, delegation_correlation_id: UUID
    ) -> ModelGoalContractRevision | None:
        return self.by_delegation.get(delegation_correlation_id)


def build_engine(
    *,
    reader: DictContractReader | None = None,
    rules_override: dict[str, object] | None = None,
) -> tuple[
    GoalVerificationEngine, StoreGoalVerificationInMemory, RecordingPublisher, FakeClock
]:
    store = StoreGoalVerificationInMemory()
    publisher = RecordingPublisher()
    clock = FakeClock()
    contracts = reader if reader is not None else DictContractReader()
    if reader is None:
        contracts.add(goal_contract())
    rules = load_rules()
    if rules_override:
        rules = rules.model_copy(update=rules_override)
    engine = GoalVerificationEngine(
        rules=rules,
        store=store,
        contract_reader=contracts,
        publisher=publisher,
        clock=clock,
    )
    return engine, store, publisher, clock
