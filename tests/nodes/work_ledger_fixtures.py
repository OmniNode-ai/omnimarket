"""Deterministic released-core records used by projection acceptance tests."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from omnibase_core.enums.enum_cost_basis import EnumCostBasis
from omnibase_core.enums.enum_hold_block import EnumHoldBlock
from omnibase_core.enums.enum_question_withdrawal_reason import (
    EnumQuestionWithdrawalReason,
)
from omnibase_core.enums.enum_work_outcome import EnumWorkOutcome
from omnibase_core.models.events.work import (
    WORK_LEDGER_SCHEMA,
    ModelEvidenceRefs,
    ModelHoldScope,
    ModelRecipients,
    ModelSessionActor,
    ModelWorkClaimRequested,
    ModelWorkCorrectionRecorded,
    ModelWorkEvent,
    ModelWorkFrictionRecorded,
    ModelWorkHoldPlaced,
    ModelWorkHoldReleased,
    ModelWorkLedgerEpochOpened,
    ModelWorkLedgerRecord,
    ModelWorkMessageAcked,
    ModelWorkMessageSent,
    ModelWorkOperatorConsentRecorded,
    ModelWorkQuestionAsked,
    ModelWorkQuestionWithdrawn,
    ModelWorkResultRecorded,
    ModelWorkRulingRecorded,
    ModelWorkStatusRecorded,
)
from omnibase_core.models.events.work.model_work_ledger_line import (
    dump_work_ledger_line,
)

AS_OF = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
EVENT_TIME = datetime(2026, 9, 28, 11, 0, tzinfo=UTC)


def event_id(number: int) -> uuid.UUID:
    return uuid.UUID(f"00000000-0000-4000-8000-{number:012d}")


def _actor(name: str = "acceptance-lane") -> ModelSessionActor:
    return ModelSessionActor(session_handle=name, agent_kind="build-lane")


def _base(number: int, summary: str = "acceptance record") -> dict[str, object]:
    return {
        "event_id": event_id(number),
        "emitted_at": EVENT_TIME,
        "actor": _actor(),
        "summary": summary,
    }


def records_for_all_ledger_row_types() -> tuple[ModelWorkLedgerRecord, ...]:
    """Build one valid record for each of the eleven rolling-ledger row types."""
    hold_id = event_id(5)
    claim_id = event_id(2)
    msg_id = event_id(6)
    friction_id = event_id(4)
    events: tuple[ModelWorkEvent, ...] = (
        ModelWorkLedgerEpochOpened(
            **_base(1),
            reason="cutover",
            epoch_seq=0,
            archived_path="docs/tracking/archive/ROLLING_WORK_LEDGER_PRE_TYPED.md",
            archived_sha256="0" * 64,
            archived_line_count=11,
            review_list_ref="beta/tracking/typed-ledger-cutover-review.md",
        ),
        ModelWorkClaimRequested(**_base(2), ticket_id="OMN-20001"),
        ModelWorkCorrectionRecorded(**_base(3), corrects=claim_id),
        ModelWorkFrictionRecorded(
            **_base(4),
            ticket_id="OMN-20001",
            cost_lane_hours=Decimal("0.5"),
            cost_basis=EnumCostBasis.ESTIMATED,
        ),
        ModelWorkHoldPlaced(
            **_base(5),
            scope=ModelHoldScope(repos=frozenset({"omnimarket"})),
            blocks=frozenset({EnumHoldBlock.MERGE}),
        ),
        ModelWorkMessageSent(
            **_base(6), to=ModelRecipients(lanes=frozenset({"acceptance-lane"}))
        ),
        ModelWorkOperatorConsentRecorded(
            **_base(7),
            operator_words="approved",
            approved_scope=("local acceptance proof",),
            out_of_scope=("runtime changes",),
        ),
        ModelWorkHoldReleased(
            **_base(8), releases=hold_id, surface_result=None, surface_restored=None
        ),
        ModelWorkRulingRecorded(**_base(9), operator_words="proceed"),
        ModelWorkStatusRecorded(**_base(10), verdict="GREEN"),
        ModelWorkResultRecorded(
            **_base(11),
            outcome=EnumWorkOutcome.TERMINAL,
            closes_claims=frozenset({claim_id}),
            friction_refs=frozenset({friction_id}),
        ),
        ModelWorkMessageAcked(**_base(12), re=msg_id),
    )
    return tuple(
        ModelWorkLedgerRecord.model_validate(
            {"schema": WORK_LEDGER_SCHEMA, "event": event}
        )
        for event in events
    )


def question_records() -> tuple[ModelWorkLedgerRecord, ...]:
    """Return OPEN, withdrawn, and withdrawn-then-answered question histories."""
    withdrawn_id = event_id(21)
    answered_id = event_id(22)
    questions: tuple[ModelWorkEvent, ...] = (
        ModelWorkQuestionAsked(**_base(20), question="Should the projection roll now?"),
        ModelWorkQuestionAsked(
            **_base(21), question="Should this question be withdrawn?"
        ),
        ModelWorkQuestionAsked(
            **_base(22), question="Should the answer outrank withdrawal?"
        ),
        ModelWorkQuestionWithdrawn(
            **_base(23),
            withdraws=withdrawn_id,
            reason=EnumQuestionWithdrawalReason.OVERTAKEN,
            evidence=ModelEvidenceRefs(tickets=frozenset({"OMN-20001"})),
        ),
        ModelWorkQuestionWithdrawn(
            **_base(24),
            withdraws=answered_id,
            reason=EnumQuestionWithdrawalReason.OVERTAKEN,
            evidence=ModelEvidenceRefs(tickets=frozenset({"OMN-20001"})),
        ),
        ModelWorkRulingRecorded(
            **_base(25), operator_words="yes", answers=frozenset({answered_id})
        ),
        ModelWorkQuestionAsked(
            **_base(26), question="Does consent answer this question?"
        ),
        ModelWorkOperatorConsentRecorded(
            **_base(27),
            operator_words="approved",
            approved_scope=("local acceptance proof",),
            out_of_scope=("runtime changes",),
            answers=frozenset({event_id(26)}),
        ),
    )
    return tuple(
        ModelWorkLedgerRecord.model_validate(
            {"schema": WORK_LEDGER_SCHEMA, "event": event}
        )
        for event in questions
    )


def core_lines(records: tuple[ModelWorkLedgerRecord, ...]) -> list[str]:
    return [dump_work_ledger_line(record) for record in records]
