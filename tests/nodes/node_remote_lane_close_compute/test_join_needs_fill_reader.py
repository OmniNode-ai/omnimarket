# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Join test for the stopped lane's needs cell and the fill that continues it (OMN-23417).

The stopped-lane compute (``node_factory_floor_compute``, lane ``contin``'s consumer of the closing row) now emits ``needs`` on
each stopped lane it lists. The reader on the other side is the fill: ``node_floor_fill_candidates_compute`` validates the same
entry as ``ModelRecordStopped`` and builds the continuation lane's brief. The fill orchestrator that sends the brief is lane
``contfill``'s change (omnimarket#5699, OMN-23416); it reads the candidate this compute returns and is not changed here.

The seam, as read today: ``ModelRecordStopped`` has no ``needs`` field and ignores extra keys, so the typed ask (action and
target) is dropped at the fill's read and the brief names ``needs`` in its "naming" list without carrying the cell. These tests
pin that reading so the join goes red when either side changes shape: the producer adds or renames a field, or the fill starts
reading ``needs``. The change that makes the fill read the cell flips ``test_seam_pin_the_fill_does_not_read_the_needs_cell_yet``
and the field-set join in the same pull request. The entry crosses the in-memory bus and, with ``CLOSING_ROW_PARITY_BOOTSTRAP``
naming a lab broker, a Redpanda broker:

    CLOSING_ROW_PARITY_BOOTSTRAP=<host:port> uv run pytest -m "closing_row_parity and kafka" -q
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest

from omnimarket.models.floor_fill import ModelFloorFillStopped
from omnimarket.models.floor_fill.model_floor_fill_candidates import (
    ModelRecordStopped,
)
from omnimarket.nodes.node_factory_floor_compute.handlers.floor_stopped import (
    build_stopped,
)
from omnimarket.nodes.node_factory_floor_compute.models.model_factory_floor_output import (
    ModelFloorStoppedLane,
)
from omnimarket.nodes.node_factory_floor_effect.source_loader import (
    parse_ledger_text,
)
from omnimarket.nodes.node_floor_fill_candidates_compute.handlers.handler_floor_fill_candidates import (
    HandlerFloorFillCandidates,
)
from omnimarket.nodes.node_floor_fill_candidates_compute.tests import (
    support as fill_support,
)
from omnimarket.nodes.node_remote_lane_start_orchestrator.tests.lane_start_fixtures import (
    T0,
)
from tests.nodes.node_factory_floor_compute.support import request as floor_request
from tests.nodes.node_remote_lane_close_compute import (
    test_join_closing_row_needs as join,
)

pytestmark = [pytest.mark.unit, pytest.mark.closing_row_parity]

DELEGATION = "DELEGATION delegated=0 reason=read-only-lane"
TYPED = (
    "The required check failed on a runner timeout.\n"
    "LANE_RESULT outcome=blocked needs=rerun:omnibase_core#2302\n" + DELEGATION
)
UNTYPED = (
    "I could not push.\nBLOCKER: someone must rerun the check\n"
    "LANE_RESULT outcome=blocked\n" + DELEGATION
)
CELL = "rerun:omnibase_core#2302"
# The producer fields the fill does not read today; the fill's change reads needs and shrinks this set.
NOT_READ_BY_THE_FILL = {"needs", "age_minutes"}


def stopped_entry(rows: dict[str, str]) -> dict[str, Any]:
    """The producer side: the closing rows through the floor effect's parser and the stopped-lane compute, as the record dumps it."""
    parsed = parse_ledger_text(f"L1 {rows['claim']}\nL2 {rows['terminal']}")
    made = floor_request(ledger=parsed, goal_scope=join.SCOPE)
    asked = made.model_copy(update={"generated_at": T0 + timedelta(minutes=5)})
    listed, _, _ = build_stopped(asked)
    [lane] = listed
    return lane.model_dump(mode="json")


def in_process() -> dict[str, dict[str, Any]]:
    return {
        "typed": stopped_entry(join.serve_close(TYPED)),
        "untyped": stopped_entry(join.serve_close(UNTYPED)),
    }


def decide_entry(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": payload["id"],
        "entry": stopped_entry(join.serve_close(payload["message"])),
    }


async def run_over(bus: join.Bus) -> dict[str, dict[str, Any]]:
    await bus.start()
    try:
        out: dict[str, dict[str, Any]] = {}
        for name, message in (("typed", TYPED), ("untyped", UNTYPED)):
            reply = await bus.call({"id": uuid.uuid4().hex, "message": message})
            out[name] = reply["entry"]
    finally:
        await bus.stop()
    return out


def fill_reads(entry: dict[str, Any]) -> list[ModelFloorFillStopped]:
    """The consuming side: the fill's candidates compute over a record that carries ``entry`` as its stopped lane."""
    record = json.loads(fill_support.record([], generated_at=fill_support.NOW))
    record["floor"] = {"lanes": [], "stopped": [entry]}
    result = HandlerFloorFillCandidates().handle(
        fill_support.request(floor_record=json.dumps(record))
    )
    return list(result.stopped)


def judge(entry: dict[str, Any], want_cell: str) -> None:
    """The join's reading: the cell the producer wrote reaches the entry, and the fill lists the lane that carries it."""
    assert entry.get("needs") == want_cell, (
        f"needs cell {entry.get('needs')!r} != {want_cell!r}"
    )
    [candidate] = fill_reads(entry)
    assert candidate.lane == entry["lane"]
    assert "needs" in candidate.brief


def break_rename_needs(entry: dict[str, Any]) -> dict[str, Any]:
    """Planted rename on the wire: the producer's ``needs`` key is spelled ``need``."""
    return {("need" if k == "needs" else k): v for k, v in entry.items()}


def break_rename_lane(entry: dict[str, Any]) -> dict[str, Any]:
    """Planted rename on the wire: the producer's ``lane`` key is spelled ``lane_id``."""
    return {("lane_id" if k == "lane" else k): v for k, v in entry.items()}


def test_golden_the_typed_ask_reaches_the_fill_as_a_listed_lane_naming_needs() -> None:
    entry = in_process()["typed"]

    judge(entry, CELL)
    assert "needs" in entry["names"]


def test_seam_pin_the_fill_does_not_read_the_needs_cell_yet() -> None:
    """Pins the reading: the fill carries the cell only as text inside the predecessor's last rows, never as a typed ask.

    ``ModelRecordStopped`` has no ``needs`` field, so the action and target are not a value the brief states; they reach the
    continuation lane only because the ending row's own text (``needs=<cell>``) is among the rows the brief quotes. The fill
    reader change (a ``needs`` field and a sentence in the brief) flips this test; update it in that pull request.
    """
    [candidate] = fill_reads(in_process()["typed"])
    lead, _, quoted = candidate.brief.partition("The predecessor's last rows:")

    assert "needs" not in ModelRecordStopped.model_fields
    assert CELL not in lead
    assert f"needs={CELL}" in quoted


def test_seam_the_producer_fields_the_fill_does_not_read_are_the_named_ones() -> None:
    unread = set(ModelFloorStoppedLane.model_fields) - set(
        ModelRecordStopped.model_fields
    )

    assert unread == NOT_READ_BY_THE_FILL


def test_error_chain_an_untyped_close_still_lists_the_lane_for_the_fill() -> None:
    """A close whose message names no typed ask reads ``needs=unparsed``; the fill still gets the lane and a brief."""
    entry = in_process()["untyped"]

    assert entry["needs"] == "unparsed"
    [candidate] = fill_reads(entry)
    assert candidate.lane == entry["lane"]
    assert candidate.brief


def test_golden_in_memory_bus_entries_match_the_in_process_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(join, "decide", decide_entry)

    carried = asyncio.run(run_over(join.InmemoryBus()))

    assert carried == in_process()
    judge(carried["typed"], CELL)


@pytest.mark.parametrize(
    "wire",
    [break_rename_needs, break_rename_lane],
    ids=lambda f: f.__name__,
)
def test_control_a_planted_rename_breaks_the_join(
    wire: Callable[[dict[str, Any]], dict[str, Any]],
) -> None:
    entry = in_process()["typed"]
    judge(
        entry, CELL
    )  # the same reading passes without the break, so the red is the break's
    with pytest.raises((AssertionError, ValueError)):
        judge(wire(entry), CELL)


@pytest.mark.kafka
@pytest.mark.integration
def test_golden_redpanda_bus_entries_match_the_in_memory_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not os.environ.get(join.BOOTSTRAP_ENV):
        pytest.skip(f"Kafka not reachable: {join.BOOTSTRAP_ENV} names no lab broker")
    monkeypatch.setattr(join, "decide", decide_entry)

    carried = asyncio.run(run_over(join.RedpandaBus(os.environ[join.BOOTSTRAP_ENV])))

    assert carried == in_process()
    judge(carried["typed"], CELL)
