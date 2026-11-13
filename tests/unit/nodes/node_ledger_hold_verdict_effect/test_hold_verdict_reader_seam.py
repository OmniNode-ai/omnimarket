# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam test: the query node's holds line shape and the verdict's parser (OMN-19609, step L6).

Two pieces share one line shape: ``node_work_ledger_query_effect`` renders each hold in force and
``node_work_ledger_reader_effect`` carries the lines; ``node_ledger_hold_verdict_effect`` parses
them. This runs the real query node handler over a state page through the real reader handler
into the verdict handler, on the in-memory path. It fails when either side changes the shape:
a renamed ``scope=<...>`` or ``until=`` cell turns HELD into UNDECIDED (never CLEAR). The same
shared model is the reader's ``ModelReaderResult``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from omnimarket.events.hold_verdict import (
    EnumHoldSource,
    EnumHoldVerdict,
    ModelHoldVerdictOverlay,
    ModelHoldVerdictRequest,
)
from omnimarket.models.work_ledger_query import ModelWorkLedgerStateEntity
from omnimarket.nodes.node_ledger_hold_verdict_effect.handlers.handler_hold_verdict import (
    HandlerHoldVerdict,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.work_ledger_query_reader import (
    WorkLedgerReadError,
)
from omnimarket.nodes.node_work_ledger_reader_effect.adapters import InMemoryEventSink
from omnimarket.nodes.node_work_ledger_reader_effect.handlers import (
    HandlerWorkLedgerReader,
)
from omnimarket.nodes.node_work_ledger_reader_effect.models import (
    EnumReaderQuery,
    EnumReaderStatus,
    ModelReaderRequest,
)
from tests.unit.nodes.node_ledger_hold_verdict_effect.test_hold_verdict_chain_events import (
    ListSink,
)
from tests.unit.nodes.node_work_ledger_query_effect.state_fixtures import (
    NOW,
    FakeStateReader,
    record,
)
from tests.unit.nodes.node_work_ledger_reader_effect.test_reader_chain_events import (
    QueryNodePointRead,
    overlay,
)

pytestmark = pytest.mark.unit

HOLD_RAW = (
    "2026-11-01T10:00:00Z | HOLD | lane=orch-1 | id=hold-a | to=lane-x "
    "| repo=omnimarket | pr=omnimarket#7 | until=2026-11-30T00:00:00Z | pause the PR"
)


def pr_hold_entity() -> ModelWorkLedgerStateEntity:
    return ModelWorkLedgerStateEntity(
        entity_key="hold:hold-a",
        kind="hold",
        lane="orch-1",
        repo="omnimarket",
        pr="omnimarket#7",
        scope_to="lane-x",
        until_at=datetime(2026, 11, 30, tzinfo=UTC),
        opened_at=datetime(2026, 11, 1, 10, tzinfo=UTC),
        opened_row=record(HOLD_RAW, "row-hold-a"),
    )


def verdict_over(reader_state: FakeStateReader, pr: str = "omnimarket#7"):
    reader = HandlerWorkLedgerReader(
        overlay(moved_readers=("pause-check",)),
        QueryNodePointRead(reader_state),
        InMemoryEventSink(),
        clock=lambda: NOW,
    )
    answer = reader.handle(
        ModelReaderRequest(reader="pause-check", query=EnumReaderQuery.HOLDS)
    )
    verdict = HandlerHoldVerdict(
        ModelHoldVerdictOverlay(file_fallback_allowed=True, journal_env="J"),
        ListSink(),
        clock=lambda: NOW,
    ).handle(ModelHoldVerdictRequest(pr=pr, lane="lane-x", node_answer=answer))
    return answer, verdict


def test_a_hold_only_in_the_database_state_answers_held_through_the_real_reader() -> (
    None
):
    answer, verdict = verdict_over(FakeStateReader((pr_hold_entity(),)))
    assert answer.status is EnumReaderStatus.OK
    assert answer.lines[0].startswith("HOLD hold-a by=orch-1 scope=<")
    assert verdict.verdict is EnumHoldVerdict.HELD
    assert verdict.source is EnumHoldSource.NODE
    assert verdict.hold_ids == ("hold-a",)


def test_the_same_state_for_another_pr_is_clear_from_the_node() -> None:
    _, verdict = verdict_over(FakeStateReader((pr_hold_entity(),)), pr="omnimarket#8")
    assert verdict.verdict is EnumHoldVerdict.CLEAR
    assert verdict.source is EnumHoldSource.NODE


def test_an_empty_state_is_clear_only_because_the_node_answered() -> None:
    answer, verdict = verdict_over(FakeStateReader(()))
    assert answer.lines == ("HOLD none checked=0 released=0 expired=0 legacy-no-id=0",)
    assert (verdict.verdict, verdict.source) == (
        EnumHoldVerdict.CLEAR,
        EnumHoldSource.NODE,
    )


def test_a_database_fault_is_undecided_never_clear() -> None:
    answer, verdict = verdict_over(
        FakeStateReader(error=WorkLedgerReadError("database unavailable"))
    )
    assert answer.status is EnumReaderStatus.UNREADABLE
    assert verdict.verdict is EnumHoldVerdict.UNDECIDED
    assert verdict.reason == "node-unreadable-and-no-file-lines"


def test_break_a_renamed_scope_cell_turns_held_into_undecided_not_clear() -> None:
    """The shape guard: if the rendered line drops ``scope=<...>`` the seam is loud, not CLEAR."""
    answer, _ = verdict_over(FakeStateReader((pr_hold_entity(),)))
    broken = answer.model_copy(
        update={
            "lines": tuple(line.replace("scope=<", "span=<") for line in answer.lines)
        }
    )
    verdict = HandlerHoldVerdict(
        ModelHoldVerdictOverlay(file_fallback_allowed=True, journal_env="J"),
        ListSink(),
        clock=lambda: NOW,
    ).handle(ModelHoldVerdictRequest(pr="omnimarket#7", node_answer=broken))
    assert verdict.verdict is EnumHoldVerdict.UNDECIDED
    assert verdict.reason == "unparseable-hold-line"
