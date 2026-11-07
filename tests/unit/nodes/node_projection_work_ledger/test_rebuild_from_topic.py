# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The accepted-row topic keeps rows forever, and an empty table fed from it equals the live table (OMN-22915, L12).

Golden chain: accepted-row events (bootstrap history, then sequenced live rows) rebuild a table that equals the
live table, row for row and sequence for sequence, and entity for entity. The live table here is built the way
the projection builds it today, from the row-type events in arrival order, so the two paths are independent.
Error chain: events that cannot be rebuilt into one table are refused. Failing control: one row withheld from
the feed makes the comparison name it, and so does a row planted in the live table alone.
"""

from __future__ import annotations

import copy
import random
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.events.model_ledger_row_event import work_ledger_row_id
from omnimarket.handlers.work_ledger_fold import apply_ops, fold_row
from omnimarket.models.model_work_ledger_fold_request import ModelWorkLedgerFoldRequest
from omnimarket.nodes.node_projection_work_ledger.contract_topics import (
    TOPIC_ACCEPTED_ROW,
)
from omnimarket.nodes.node_work_ledger_reducer_compute.logic.work_ledger_rebuild import (
    HandlerWorkLedgerRebuild,
    WorkLedgerRebuildError,
)
from omnimarket.nodes.node_work_ledger_reducer_compute.models.model_work_ledger_rebuild import (
    EnumAcceptedRowSource,
    ModelAcceptedRowEvent,
    ModelRebuildRequest,
    ModelRebuildResult,
    ModelRebuiltEntity,
    ModelWorkLedgerTable,
)

pytestmark = pytest.mark.unit

_CONTRACT = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_projection_work_ledger/contract.yaml"
)
LEDGER_ID = "rolling-work-ledger"

ROWS = [
    '2026-09-28T09:00:00Z | RULING | lane=orch | ticket=OMN-1 | "go" | text',
    "2026-09-28T09:30:00Z | CLAIM | lane=alpha | ticket=OMN-1 | est ~1 lane-hours; displaces x; (OMN-1) | work",
    "2026-09-28T10:05:00Z | STATUS | lane=alpha | ticket=OMN-1 | progress",
    "2026-09-28T10:10:00Z | HOLD | lane=beta | id=2026-09-28T10:10:00Z-beta | surface=lab-dev | until=2026-09-28T12:00:00Z | reserved",
    "2026-09-28T10:15:00Z | MSG | from=alpha | to=beta,gamma | id=2026-09-28T10:15:00Z-alpha | look",
    "2026-09-28T10:16:00Z | ACK | from=beta | to=alpha | id=2026-09-28T10:16:00Z-beta | re=2026-09-28T10:15:00Z-alpha",
    "2026-09-28T10:30:00Z | TERMINAL | lane=alpha | ticket=OMN-1 | friction=none | done",
    "2026-09-28T11:00:00Z | RELEASE | lane=beta | re=2026-09-28T10:10:00Z-beta | surface=lab-dev | result=PASS | restored=yes | done",
    "2026-09-28T11:30:00Z | CLAIM | lane=alpha | ticket=OMN-2 | est ~1 lane-hours; displaces x; (OMN-2) | again",
]
BOOTSTRAP_ROWS = (
    3  # the first three are history before the watermark: no sequence, a legacy ordinal
)


def _event(index: int, row: str) -> ModelAcceptedRowEvent:
    if index < BOOTSTRAP_ROWS:
        return ModelAcceptedRowEvent(
            ledger_id=LEDGER_ID,
            row_id=work_ledger_row_id(row),
            raw_row=row,
            source=EnumAcceptedRowSource.BOOTSTRAP,
            legacy_ordinal=index + 1,
        )
    return ModelAcceptedRowEvent(
        ledger_id=LEDGER_ID,
        row_id=work_ledger_row_id(row),
        raw_row=row,
        source=EnumAcceptedRowSource.LIVE,
        ledger_seq=index - BOOTSTRAP_ROWS + 1,
    )


def _topic() -> tuple[ModelAcceptedRowEvent, ...]:
    return tuple(_event(i, row) for i, row in enumerate(ROWS))


def _live_table(shuffle_seed: int = 7) -> ModelWorkLedgerTable:
    """The live table as the projection builds it: row-type events in arrival order, guarded upserts."""
    events = list(_topic())
    random.Random(shuffle_seed).shuffle(events)
    rows = {}
    state: dict[str, dict[str, Any]] = {}
    for event in events:
        folded = fold_row(
            ModelWorkLedgerFoldRequest(
                raw_row=event.raw_row,
                ledger_id=LEDGER_ID,
                source="onex-ledger",
                ledger_seq=event.ledger_seq,
            )
        )
        rows[folded.row.row_id] = folded.row
        apply_ops(state, folded.ops)
    return ModelWorkLedgerTable(
        rows=tuple(rows.values()),
        state=tuple(
            ModelRebuiltEntity(entity_key=k, **v) for k, v in sorted(state.items())
        ),
    )


def _rebuild(
    events: tuple[ModelAcceptedRowEvent, ...], live: ModelWorkLedgerTable | None = None
) -> ModelRebuildResult:
    return HandlerWorkLedgerRebuild().handle(
        ModelRebuildRequest(events=events, live=live)
    )


# --- the broker declaration -------------------------------------------------------------------


def _unlimited_retention(contract: dict[str, Any], topic: str) -> bool:
    declared = [e for e in contract.get("published_events", []) if e["topic"] == topic]
    if len(declared) != 1:
        return False
    config = declared[0].get("topic_config", {}).get("kafka_config", {})
    return bool(
        config.get("retention.ms") == "-1"
        and config.get("retention.bytes") == "-1"
        and config.get("cleanup.policy") == "delete"
    )


def test_the_accepted_row_topic_is_declared_with_retention_minus_one() -> None:
    contract = yaml.safe_load(_CONTRACT.read_text())
    assert TOPIC_ACCEPTED_ROW == "onex.evt.omnimarket.work-ledger-row-accepted.v1"
    assert TOPIC_ACCEPTED_ROW in contract["event_bus"]["publish_topics"]
    assert _unlimited_retention(contract, TOPIC_ACCEPTED_ROW)
    declared = next(
        e for e in contract["published_events"] if e["topic"] == TOPIC_ACCEPTED_ROW
    )
    assert declared["topic_config"]["partitions"] == 1, (
        "one partition: the offset is the order"
    )


def test_failing_control_a_bounded_declaration_does_not_read_as_unlimited() -> None:
    contract = yaml.safe_load(_CONTRACT.read_text())
    for key, bad in (("retention.ms", "604800000"), ("retention.bytes", "1073741824")):
        broken = copy.deepcopy(contract)
        entry = next(
            e for e in broken["published_events"] if e["topic"] == TOPIC_ACCEPTED_ROW
        )
        entry["topic_config"]["kafka_config"][key] = bad
        assert not _unlimited_retention(broken, TOPIC_ACCEPTED_ROW), key
    removed = copy.deepcopy(contract)
    removed["published_events"] = []
    assert not _unlimited_retention(removed, TOPIC_ACCEPTED_ROW)


# --- the rebuild --------------------------------------------------------------------------------


def test_golden_a_rebuild_from_the_topic_equals_the_live_table_row_for_row() -> None:
    live = _live_table()
    result = _rebuild(_topic(), live)
    assert result.compared is True
    assert result.differences == ()
    assert result.equal is True
    assert len(result.rebuilt.rows) == len(ROWS) == len(live.rows)
    assert [r.ledger_seq for r in result.rebuilt.rows] == [
        1,
        2,
        3,
        4,
        5,
        6,
        None,
        None,
        None,
    ]
    assert {e.entity_key: e.is_open for e in result.rebuilt.state}[
        "claim:alpha"
    ] is True
    assert {e.entity_key for e in result.rebuilt.state} == {
        e.entity_key for e in live.state
    }


def test_the_rebuild_is_the_same_whatever_order_the_events_arrive_in() -> None:
    events = list(_topic())
    random.Random(3).shuffle(events)
    assert _rebuild(tuple(events)).rebuilt == _rebuild(_topic()).rebuilt


def test_a_replayed_row_adds_nothing() -> None:
    events = _topic()
    assert _rebuild((*events, events[4], events[0])).rebuilt == _rebuild(events).rebuilt


def test_without_a_live_table_the_rebuild_reports_it_compared_nothing() -> None:
    result = _rebuild(_topic())
    assert (result.compared, result.equal, result.differences) == (False, False, ())


def test_failing_control_one_row_withheld_from_the_feed_is_named() -> None:
    events = _topic()
    withheld = events[5]
    result = _rebuild(events[:5] + events[6:], _live_table())
    assert result.equal is False
    rows = [d for d in result.differences if d.where == "rows"]
    assert [(d.key, d.difference) for d in rows] == [
        (withheld.row_id, "in the live table, absent from the rebuild")
    ]


def test_failing_control_a_row_planted_in_the_live_table_alone_is_named() -> None:
    live = _live_table()
    planted = fold_row(
        ModelWorkLedgerFoldRequest(
            raw_row="2026-09-28T12:00:00Z | STATUS | lane=alpha | ticket=OMN-1 | planted",
            ledger_id=LEDGER_ID,
            source="onex-ledger",
            ledger_seq=99,
        )
    ).row
    result = _rebuild(_topic(), live.model_copy(update={"rows": (*live.rows, planted)}))
    assert result.equal is False
    assert [(d.key, d.difference) for d in result.differences] == [
        (planted.row_id, "in the live table, absent from the rebuild")
    ]


def test_a_sequence_that_differs_is_named_by_column() -> None:
    live = _live_table()
    moved = live.rows[0].model_copy(update={"ledger_seq": 50})
    rows = tuple(moved if r.row_id == moved.row_id else r for r in live.rows)
    result = _rebuild(_topic(), live.model_copy(update={"rows": rows}))
    assert result.equal is False
    assert len(result.differences) == 1
    assert result.differences[0].difference.startswith("ledger_seq: rebuild ")


# --- the error chain ----------------------------------------------------------------------------


def test_error_a_row_id_that_is_not_the_hash_of_its_text_is_refused() -> None:
    bad = _topic()[4].model_copy(update={"row_id": "0" * 64})
    with pytest.raises(WorkLedgerRebuildError, match="not the hash of its raw_row"):
        _rebuild((bad,))


def test_error_one_sequence_claimed_by_two_rows_is_refused() -> None:
    events = list(_topic())
    events[5] = events[5].model_copy(update={"ledger_seq": events[4].ledger_seq})
    with pytest.raises(WorkLedgerRebuildError, match="is claimed by rows"):
        _rebuild(tuple(events))


def test_error_one_row_under_two_sequences_is_refused() -> None:
    events = _topic()
    moved = events[4].model_copy(update={"ledger_seq": 40})
    with pytest.raises(WorkLedgerRebuildError, match="accepted under two sequences"):
        _rebuild((*events, moved))


def test_error_an_event_with_no_place_is_refused() -> None:
    placeless = _topic()[4].model_copy(update={"ledger_seq": None})
    with pytest.raises(
        WorkLedgerRebuildError, match="neither legacy_ordinal nor ledger_seq"
    ):
        _rebuild((placeless,))


def test_error_an_accepted_row_that_does_not_fold_is_refused() -> None:
    row = "not a canonical ledger row"
    event = ModelAcceptedRowEvent(
        ledger_id=LEDGER_ID,
        row_id=work_ledger_row_id(row),
        raw_row=row,
        source=EnumAcceptedRowSource.LIVE,
        ledger_seq=1,
    )
    with pytest.raises(WorkLedgerRebuildError, match="does not fold"):
        _rebuild((event,))
