"""Question status acceptance under reordered bus delivery."""

from __future__ import annotations

import random

import pytest
from omnibase_core.enums.enum_question_status import EnumQuestionStatus
from omnibase_core.models.events.work.model_work_ledger_render import (
    ROW_TYPE_BY_KIND,
    render_ledger_row,
)

from omnimarket.events.enum_ledger_row_type import EnumLedgerRowType
from omnimarket.events.model_ledger_row_event import work_ledger_row_id
from omnimarket.nodes.node_projection_work_ledger.handlers.handler_work_ledger_projection import (
    HandlerProjectionWorkLedger,
    WorkLedgerProjectionWriter,
)
from omnimarket.nodes.node_projection_work_ledger.models import (
    ModelWorkLedgerProjectionInbound,
)
from omnimarket.projection.runner import MessageMeta
from tests.nodes.test_work_ledger_event_identity import (
    source_database as source_database,
)
from tests.nodes.work_ledger_fixtures import (
    AS_OF,
    event_id,
    question_records,
    records_for_all_ledger_row_types,
)


def test_question_answer_withdrawal_and_open_states_ignore_delivery_order() -> None:
    base_records = records_for_all_ledger_row_types()
    question_history = question_records()
    records = (*base_records[:1], *question_history)
    projector = HandlerProjectionWorkLedger()

    expected = {
        event_id(20): EnumQuestionStatus.OPEN,
        event_id(21): EnumQuestionStatus.WITHDRAWN,
        event_id(22): EnumQuestionStatus.ANSWERED,
        event_id(26): EnumQuestionStatus.ANSWERED,
    }
    for seed in (0, 5, 19, 2026):
        shuffled = list(records)
        random.Random(seed).shuffle(shuffled)
        state = projector.fold_records(shuffled, as_of=AS_OF)
        actual = {
            question.question.event_id: question.status for question in state.questions
        }
        assert actual == expected


@pytest.mark.integration
async def test_real_question_projection_matches_core_after_reordered_prefixes(
    source_database,
):
    db, conn, schema = source_database
    writer = WorkLedgerProjectionWriter()
    writer._db = db
    records = question_records()
    index = {record.event.event_id: record.event for record in records}
    shuffled = [*records, records[2], records[-1]]
    random.Random(19).shuffle(shuffled)
    history = []
    for offset, record in enumerate(shuffled):
        history.append(record)
        event = record.event
        raw = render_ledger_row(event, index)
        inbound = ModelWorkLedgerProjectionInbound(
            ledger_id="rolling-work-ledger",
            event_id=event.event_id,
            row_id=work_ledger_row_id(raw),
            raw_row=raw,
            source="test",
            provenance_kind="typed",
            event=event,
        )
        topic = EnumLedgerRowType(ROW_TYPE_BY_KIND[event.kind]).typed_topic
        await writer._project_and_report(
            topic,
            inbound.model_dump(mode="json"),
            MessageMeta(partition=0, offset=offset, fallback_id="", topic=topic),
        )
        expected = HandlerProjectionWorkLedger.fold_records(history, as_of=AS_OF)
        rows = await conn.fetch(
            f"SELECT question_event, question_status FROM {schema}.work_ledger_state WHERE kind='question'"
        )
        assert {row["question_event"]: row["question_status"] for row in rows} == {
            q.question.event_id: q.status.value for q in expected.questions
        }
    assert (
        await conn.fetchval(
            f"SELECT question_status FROM {schema}.work_ledger_state WHERE question_event=$1",
            event_id(22),
        )
        == EnumQuestionStatus.ANSWERED.value
    )

    # A stale OPEN snapshot from an earlier prefix must not overwrite ANSWERED.
    from datetime import UTC, datetime

    from omnimarket.nodes.node_projection_work_ledger.handlers import (
        handler_work_ledger_projection as module,
    )

    question = next(
        record.event for record in records if record.event.event_id == event_id(22)
    )
    await conn.execute(
        module._UPSERT_QUESTION,
        f"question:{question.event_id}",
        question.actor.actor_key,
        question.question,
        question.emitted_at,
        str(question.event_id),
        datetime.now(UTC),
        EnumQuestionStatus.OPEN.value,
        question.event_id,
        "stale prefix",
    )
    assert (
        await conn.fetchval(
            f"SELECT question_status FROM {schema}.work_ledger_state WHERE question_event=$1",
            question.event_id,
        )
        == EnumQuestionStatus.ANSWERED.value
    )
