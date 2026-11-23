# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing tick is the caller of node_running_read_due_compute (OMN-26549).

The running-read due node had no caller: nothing wrote its row at a merge and nothing read the due rows at a tick,
so the ledger held no kind=running-read-due row. The tick now maps each merged runtime-affecting pull request it
carries to the node's ``write_due_row`` request and, when it carries the ledger text, asks ``read_due_rows`` at its
own clock. The requests go to the tick's ledger appender, the door to the work ledger append effect, in act mode
only; observe and replay record would_send and append nothing. The appender publishes on the writer topic the append
effect's own contract derives for the tick's login, never on the legacy command topic its serve refuses.

The fixture is a recorded merge: omnimarket#6623 merged at 2026-11-23T03:45:03Z. The writer topic is derived from the
append effect's real contract.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml

from omnimarket.handlers import ledger_row_grammar as grammar
from omnimarket.models.landing_controller_carry import decisions_of
from omnimarket.models.work_ledger_append import ModelWorkLedgerAppendRequest
from omnimarket.nodes.node_pr_landing_tick_orchestrator.handlers.handler_pr_landing_tick_orchestrator import (
    HandlerPrLandingTickOrchestrator,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.handlers.tick_ports import (
    InMemoryTickLedgerAppender,
    InMemoryTickPublisher,
    InMemoryTickWriter,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.handlers.tick_running_read import (
    WriterTopicLedgerAppender,
    writer_topic_for,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.models.model_tick_orchestrator import (
    EnumTickMode,
    ModelTickRequest,
    ModelTickResult,
)
from omnimarket.nodes.node_running_read_due_compute.models.model_running_read_due import (
    ModelMergedPrFact,
)
from tests.nodes.node_pr_landing_snapshot_effect.controller_inputs import (
    carry,
    node_facts_bare,
)

pytestmark = pytest.mark.unit

NODES = Path(__file__).resolve().parents[3] / "src" / "omnimarket" / "nodes"
MERGED_AT = datetime(2026, 11, 23, 3, 45, 3, tzinfo=UTC)
DUE_ID = "rrd:OmniNode-ai/omnimarket:6623"


def _contract(node: str) -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(
        (NODES / node / "contract.yaml").read_text(encoding="utf-8")
    )
    return loaded


def _merged(*, runtime_affecting: bool = True) -> ModelMergedPrFact:
    return ModelMergedPrFact(
        pr="OmniNode-ai/omnimarket#6623",
        ticket="OMN-26491",
        owner="orchestrator-31d9-swarm",
        merged_at=MERGED_AT,
        runtime_affecting=runtime_affecting,
        pr_body="Running read: node=node_running_read_due_compute topic=none.declared",
    )


def _request(
    mode: EnumTickMode,
    *,
    now: datetime,
    merged: tuple[ModelMergedPrFact, ...] = (),
    ledger_text: str | None = None,
) -> ModelTickRequest:
    return ModelTickRequest(
        correlation_id=uuid4(),
        mode=mode,
        facts=node_facts_bare().model_copy(update={"observed_at": now}),
        controller=decisions_of(carry()),
        merged_prs=merged,
        running_read_ledger_text=ledger_text,
    )


def _handler(
    appender: InMemoryTickLedgerAppender,
    publisher: InMemoryTickPublisher | None = None,
) -> HandlerPrLandingTickOrchestrator:
    return HandlerPrLandingTickOrchestrator(
        publisher=publisher or InMemoryTickPublisher(),
        writer=InMemoryTickWriter(),
        act_enabled=True,
        ledger_appender=appender,
    )


def _appended(
    appender: InMemoryTickLedgerAppender,
) -> list[ModelWorkLedgerAppendRequest]:
    return appender.appended


def _row_of(request: ModelWorkLedgerAppendRequest) -> grammar.Row:
    row = grammar.parse_row(request.rows)
    assert row is not None
    return row


def test_the_appender_publishes_on_the_writer_topic_not_the_legacy_command_topic() -> (
    None
):
    legacy = _contract("node_work_ledger_append_effect")["runtime_dispatch"][
        "command_topic"
    ]
    sink = InMemoryTickPublisher()
    appender = WriterTopicLedgerAppender(sink, principal="h051-landing-tick")
    request = _due_request()
    appender.append(request)

    assert (
        appender.topic
        == "onex.cmd.omnimarket.work-ledger-append-from-h051-landing-tick.v1"
    )
    assert appender.topic == writer_topic_for("h051-landing-tick")
    assert appender.topic != legacy
    assert sink.published == [(appender.topic, request)]
    with pytest.raises(ValueError, match="invalid writer principal"):
        writer_topic_for("Not A Principal")


def test_act_tick_requests_one_due_row_for_the_recorded_merge() -> None:
    appender = InMemoryTickLedgerAppender()
    result = _handler(appender).handle(
        _request(EnumTickMode.ACT, now=MERGED_AT, merged=(_merged(),))
    )

    sent = _appended(appender)
    assert len(sent) == 1, "the tick requested no due row for a merged runtime PR"
    row = _row_of(sent[0])
    assert row.row_type == "STATUS"
    assert row.value("kind") == "running-read-due"
    assert row.value("id") == DUE_ID
    assert row.value("pr") == "OmniNode-ai/omnimarket#6623"
    assert row.value("due") == "2026-11-23T04:45:03Z"
    assert sent[0].append_if_absent_row_id == DUE_ID
    assert result.running_read is not None
    (item,) = result.running_read.items
    assert (item.action, item.due_id, item.sent, item.would_send) == (
        "due_row",
        DUE_ID,
        True,
        False,
    )


def test_a_second_tick_for_the_same_merge_requests_no_second_row() -> None:
    appender = InMemoryTickLedgerAppender()
    handler = _handler(appender)
    request = _request(EnumTickMode.ACT, now=MERGED_AT, merged=(_merged(),))
    handler.handle(request)
    again = handler.handle(request.model_copy(update={"correlation_id": uuid4()}))

    assert len(_appended(appender)) == 1
    assert again.running_read is not None
    assert again.running_read.items == ()


def test_a_new_process_finds_the_row_on_the_ledger_text_and_requests_none() -> None:
    first = InMemoryTickLedgerAppender()
    _handler(first).handle(
        _request(EnumTickMode.ACT, now=MERGED_AT, merged=(_merged(),))
    )
    ledger_text = _appended(first)[0].rows + "\n"

    fresh = InMemoryTickLedgerAppender()
    _handler(fresh).handle(
        _request(
            EnumTickMode.ACT,
            now=MERGED_AT + timedelta(minutes=5),
            merged=(_merged(),),
            ledger_text=ledger_text,
        )
    )

    assert _appended(fresh) == []


def test_observe_tick_names_the_row_it_would_send_and_publishes_nothing_there() -> None:
    appender = InMemoryTickLedgerAppender()
    result = _handler(appender).handle(
        _request(EnumTickMode.OBSERVE, now=MERGED_AT, merged=(_merged(),))
    )

    assert _appended(appender) == []
    assert result.running_read is not None
    (item,) = result.running_read.items
    assert (item.due_id, item.sent, item.would_send) == (DUE_ID, False, True)


def test_a_merge_that_is_not_runtime_affecting_requests_no_row() -> None:
    appender = InMemoryTickLedgerAppender()
    _handler(appender).handle(
        _request(
            EnumTickMode.ACT,
            now=MERGED_AT,
            merged=(_merged(runtime_affecting=False),),
        )
    )

    assert _appended(appender) == []


def _due_request() -> ModelWorkLedgerAppendRequest:
    appender = InMemoryTickLedgerAppender()
    _handler(appender).handle(
        _request(EnumTickMode.ACT, now=MERGED_AT, merged=(_merged(),))
    )
    return appender.appended[0]


def _due_row_text() -> str:
    return _due_request().rows + "\n"


def test_before_the_due_time_the_read_lists_the_open_row_and_raises_no_alarm() -> None:
    appender = InMemoryTickLedgerAppender()
    result: ModelTickResult = _handler(appender).handle(
        _request(
            EnumTickMode.ACT,
            now=MERGED_AT + timedelta(minutes=30),
            ledger_text=_due_row_text(),
        )
    )

    assert _appended(appender) == []
    assert result.running_read is not None
    assert result.running_read.open_due_ids == (DUE_ID,)
    assert result.running_read.blocked_tickets == ("OMN-26491",)


def test_once_the_due_time_passes_the_read_raises_one_alarm_and_only_one() -> None:
    appender = InMemoryTickLedgerAppender()
    handler = _handler(appender)
    request = _request(
        EnumTickMode.ACT,
        now=MERGED_AT + timedelta(minutes=61),
        ledger_text=_due_row_text(),
    )
    result = handler.handle(request)
    handler.handle(request.model_copy(update={"correlation_id": uuid4()}))

    sent = _appended(appender)
    assert len(sent) == 1, "the due time passed with no alarm, or a second alarm"
    row = _row_of(sent[0])
    assert row.row_type == "MSG"
    assert row.value("kind") == "running-read-alarm"
    assert row.value("to") == "orchestrator-31d9-swarm"
    assert row.value("re") == DUE_ID
    assert result.running_read is not None
    assert [(i.action, i.due_id, i.sent) for i in result.running_read.items] == [
        ("alarm", DUE_ID, True)
    ]


def test_a_recorded_snapshot_with_the_merge_runs_through_the_unit_pass() -> None:
    from omnimarket.nodes.node_pr_landing_tick_orchestrator.handlers.tick_sources import (
        InMemorySnapshotSource,
        run_snapshots,
    )
    from omnimarket.nodes.node_pr_landing_tick_orchestrator.models.model_tick_orchestrator import (
        ModelTickSnapshot,
    )

    appender = InMemoryTickLedgerAppender()
    snapshot = ModelTickSnapshot(
        facts=node_facts_bare().model_copy(update={"observed_at": MERGED_AT}),
        controller=decisions_of(carry()),
        merged_prs=(_merged(),),
    )
    # observe: the unit pass of today. The step names the row it would send; nothing is published on the append topic.
    (observed,) = run_snapshots(
        _handler(appender), InMemorySnapshotSource([snapshot]), EnumTickMode.OBSERVE
    )
    assert observed.running_read is not None
    assert [(i.due_id, i.would_send, i.sent) for i in observed.running_read.items] == [
        (DUE_ID, True, False)
    ]
    assert _appended(appender) == []
    # act: the same snapshot sends exactly one request.
    (acted,) = run_snapshots(
        _handler(appender), InMemorySnapshotSource([snapshot]), EnumTickMode.ACT
    )
    assert acted.running_read is not None
    assert [(i.due_id, i.sent) for i in acted.running_read.items] == [(DUE_ID, True)]
    assert len(_appended(appender)) == 1
