# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from pydantic import TypeAdapter

from omnimarket.events.model_ledger_row_event import (
    ModelLedgerRowEvent,
    ModelLedgerTerminalEvent,
)
from omnimarket.handlers.ledger_row_parser import (
    parse_ledger_row,
)
from omnimarket.models.done_items import ModelDoneItemsRequest
from omnimarket.nodes.node_done_items_compute.handlers.handler_done_items import (
    HandlerDoneItems,
)

pytestmark = pytest.mark.unit

_EVENT: TypeAdapter[ModelLedgerRowEvent] = TypeAdapter(ModelLedgerRowEvent)


def _events(*rows: str) -> tuple[ModelLedgerRowEvent, ...]:
    return tuple(_EVENT.validate_python(parse_ledger_row(row)) for row in rows)


def _terminal(event: ModelLedgerRowEvent) -> ModelLedgerTerminalEvent:
    assert isinstance(event, ModelLedgerTerminalEvent)
    return event


def test_done_items_from_ledger_events() -> None:
    rows = (
        "2026-11-17T10:00:00Z | TERMINAL | lane=alpha | ticket=OMN-1 | outcome=done | running_path=onex x | finished",
        "2026-11-17T09:00:00Z | STATUS | lane=alpha | ticket=OMN-2 | repo=omnimarket | pr=5 | state=merged",
        "2026-11-17T09:30:00Z | TERMINAL | lane=beta | ticket=OMN-3 | outcome=handed-off | finished",
    )
    events = _events(*rows)
    projection = HandlerDoneItems().handle(ModelDoneItemsRequest(events=events))

    terminal_event = _terminal(events[0])
    assert projection.done == 1
    assert len(projection.items) == 1

    item = projection.items[0]
    assert item.ticket == "OMN-1"
    assert item.done_at == terminal_event.row_timestamp
    assert item.done_row_id == terminal_event.row_id
    assert item.lane == terminal_event.row_lane
    assert item.pr == terminal_event.pr
    assert item.closes == terminal_event.closes

    tickets = {done_item.ticket for done_item in projection.items}
    assert "OMN-2" not in tickets
    assert "OMN-3" not in tickets
    assert projection.as_of == max(event.row_timestamp for event in events)


def test_done_items_newest_done_event_wins() -> None:
    early = "2026-11-17T10:00:00Z | TERMINAL | lane=alpha | ticket=OMN-1 | outcome=done | running_path=onex x | finished"
    late = "2026-11-17T11:00:00Z | TERMINAL | lane=beta | ticket=OMN-1 | outcome=done | pr=7 | closes=OMN-9"
    events = _events(early, late)
    projection = HandlerDoneItems().handle(ModelDoneItemsRequest(events=events))

    late_event = _terminal(events[1])
    assert projection.done == 1
    assert len(projection.items) == 1

    item = projection.items[0]
    assert item.ticket == "OMN-1"
    assert item.done_at == late_event.row_timestamp
    assert item.done_row_id == late_event.row_id
    assert item.lane == late_event.row_lane
    assert item.pr == late_event.pr
    assert item.closes == late_event.closes
    assert projection.as_of == max(event.row_timestamp for event in events)


def test_done_projection_replay_same_result() -> None:
    rows = (
        "2026-11-17T10:00:00Z | TERMINAL | lane=alpha | ticket=OMN-1 | outcome=done | running_path=onex x | finished",
        "2026-11-17T09:00:00Z | STATUS | lane=alpha | ticket=OMN-2 | repo=omnimarket | pr=5 | state=merged",
        "2026-11-17T09:30:00Z | TERMINAL | lane=beta | ticket=OMN-3 | outcome=handed-off | finished",
    )
    events = _events(*rows)
    handler = HandlerDoneItems()

    first = handler.handle(ModelDoneItemsRequest(events=events))
    second = handler.handle(ModelDoneItemsRequest(events=events))
    assert first.model_dump_json() == second.model_dump_json()

    replayed = handler.handle(ModelDoneItemsRequest(events=tuple(reversed(events))))
    assert first.model_dump_json() == replayed.model_dump_json()


def test_outcome_that_only_resembles_done_does_not_count() -> None:
    # Merged is not done, and neither is an outcome that merely contains the word: only a first word exactly "done".
    rows = (
        "2026-11-17T10:00:00Z | TERMINAL | lane=alpha | ticket=OMN-1 | outcome=undone | finished",
        "2026-11-17T10:01:00Z | TERMINAL | lane=alpha | ticket=OMN-2 | outcome=doneness | finished",
        "2026-11-17T10:02:00Z | TERMINAL | lane=alpha | ticket=OMN-3 | finished",
        "2026-11-17T10:03:00Z | TERMINAL | lane=alpha | ticket=OMN-4 | outcome=done | finished",
    )
    projection = HandlerDoneItems().handle(ModelDoneItemsRequest(events=_events(*rows)))

    assert [item.ticket for item in projection.items] == ["OMN-4"]
    assert projection.done == 1


def test_same_second_done_rows_pick_one_winner_in_either_order() -> None:
    # Two done rows for one ticket in the same second: the row id breaks the tie, so the order they arrive in does not.
    first = "2026-11-17T10:00:00Z | TERMINAL | lane=alpha | ticket=OMN-1 | outcome=done | pr=1 | finished"
    second = "2026-11-17T10:00:00Z | TERMINAL | lane=beta | ticket=OMN-1 | outcome=done | pr=2 | finished"
    events = _events(first, second)
    winner = max((_terminal(event) for event in events), key=lambda event: event.row_id)

    forward = HandlerDoneItems().handle(ModelDoneItemsRequest(events=events))
    backward = HandlerDoneItems().handle(
        ModelDoneItemsRequest(events=tuple(reversed(events)))
    )

    assert forward.items[0].done_row_id == winner.row_id
    assert forward.model_dump_json() == backward.model_dump_json()


def test_one_done_row_closing_two_tickets_counts_both() -> None:
    row = "2026-11-17T10:00:00Z | TERMINAL | lane=alpha | ticket=OMN-1,OMN-2 | outcome=done | finished"
    projection = HandlerDoneItems().handle(ModelDoneItemsRequest(events=_events(row)))

    assert [item.ticket for item in projection.items] == ["OMN-1", "OMN-2"]
    assert projection.done == 2


def test_outcome_done_with_extra_words_counts() -> None:
    row = "2026-11-17T10:00:00Z | TERMINAL | lane=alpha | ticket=OMN-1 | outcome=done delegated=1 | finished"
    events = _events(row)
    projection = HandlerDoneItems().handle(ModelDoneItemsRequest(events=events))

    assert projection.done == 1
    assert len(projection.items) == 1
    assert projection.items[0].ticket == "OMN-1"
    assert projection.as_of == max(event.row_timestamp for event in events)


def test_runtime_dispatch_names_a_real_caller() -> None:
    """The contract's runtime_dispatch names a caller that exists and calls the fold.

    The old text named a done-items publish effect that does not exist.
    """
    nodes = Path(__file__).resolve().parents[3] / "src" / "omnimarket" / "nodes"
    contract = yaml.safe_load(
        (nodes / "node_done_items_compute" / "contract.yaml").read_text(
            encoding="utf-8"
        )
    )
    trigger = contract["runtime_dispatch"]["external_trigger"]
    callers = sorted(
        set(re.findall(r"node_[a-z0-9_]+", trigger)) - {"node_done_items_compute"}
    )
    modules = sorted(set(re.findall(r"omnimarket\.handlers\.[a-z0-9_]+", trigger)))
    assert callers, trigger
    assert modules, trigger
    for caller in callers:
        sources = [
            py.read_text(encoding="utf-8") for py in (nodes / caller).rglob("*.py")
        ]
        assert sources, f"{caller} does not exist"
        assert any(
            f"from {module} import" in text for text in sources for module in modules
        ), f"{caller} does not import {modules}"
