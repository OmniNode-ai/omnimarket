# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Tests for the done-items projection.

Each test catches a distinct failure:
- test_done_items_exposure_declared: the contract does not declare the expected
  bus-backed done-items exposure, columns, or key columns.
- test_done_items_exposure_served: a served projection read does not return the
  expected done tickets and field values.
- test_done_items_writer_replay: replaying or reordering done events changes the
  projected rows.
- test_done_items_older_done_event_does_not_replace: an older done event replaces
  a newer done event.
- test_done_items_v2_payload_served: a v2 terminal payload is not projected.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.events.ledger_row_parser import (
    parse_ledger_row,
)
from omnimarket.events.model_ledger_row_event import (
    work_ledger_event_id,
    work_ledger_row_id,
)
from omnimarket.nodes.node_projection_done_items.handlers.handler_done_items_writer import (
    DoneItemsProjectionWriter,
)
from omnimarket.nodes.node_projection_done_items.ports.done_items_store import (
    SqliteDoneItemsStore,
)
from omnimarket.nodes.node_projection_read_effect.handlers.handler_projection_read import (
    HandlerProjectionRead,
)
from omnimarket.nodes.node_projection_read_effect.models import (
    ModelProjectionReadRequest,
)
from omnimarket.nodes.node_projection_work_ledger.models import (
    ModelWorkLedgerProjectionInbound,
)
from omnimarket.projection.discovery import load_projection_exposures_from_contract
from omnimarket.projection.models import ProjectionTableConfig
from tests.nodes.work_ledger_fixtures import records_for_all_ledger_row_types

pytestmark = pytest.mark.unit

CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_projection_done_items/contract.yaml"
)
V1 = "onex.evt.omnimarket.work-ledger-terminal.v1"
V2 = "onex.evt.omnimarket.work-ledger-terminal.v2"
TOPIC = "onex.snapshot.projection.done-items.v1"

Payload = dict[str, Any]
Rows = dict[str, dict[str, Any]]
Served = tuple[DoneItemsProjectionWriter, HandlerProjectionRead]

DONE1 = "2026-11-17T10:00:00Z | TERMINAL | lane=alpha | ticket=OMN-1 | outcome=done | running_path=onex x | finished"
HANDED = "2026-11-17T09:30:00Z | TERMINAL | lane=beta | ticket=OMN-3 | outcome=handed-off | finished"
DONE_TWO = "2026-11-17T10:30:00Z | TERMINAL | lane=gamma | ticket=OMN-2,OMN-4 | outcome=done delegated=1 | pr=9 | finished"
DONE1_LATER = "2026-11-17T11:00:00Z | TERMINAL | lane=beta | ticket=OMN-1 | outcome=done | pr=7 | closes=OMN-9"


def _v1(raw_row: str) -> Payload:
    return {
        **parse_ledger_row(raw_row).model_dump(mode="json"),
        "event_id": str(
            work_ledger_event_id("rolling-work-ledger", work_ledger_row_id(raw_row))
        ),
    }


def _v2(raw_row: str) -> Payload:
    row_id = work_ledger_row_id(raw_row)
    eid = work_ledger_event_id("rolling-work-ledger", row_id)
    event = records_for_all_ledger_row_types()[10].event.model_copy(
        update={"event_id": eid}
    )
    return ModelWorkLedgerProjectionInbound(
        ledger_id="rolling-work-ledger",
        row_id=row_id,
        event_id=eid,
        source="acceptance-test",
        raw_row=raw_row,
        provenance_kind="markdown",
        event=event,
    ).model_dump(mode="json")


def _feed(writer: DoneItemsProjectionWriter, items: list[tuple[str, Payload]]) -> None:
    for i, (topic, payload) in enumerate(items):
        writer.handle({**payload, "_topic": topic, "_partition": 0, "_offset": i})


def _topic_map() -> dict[str, ProjectionTableConfig]:
    contract = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    return {
        e.topic: e
        for e in load_projection_exposures_from_contract(
            contract, "projection_done_items", CONTRACT
        )
    }


async def _read(reader: HandlerProjectionRead) -> Rows:
    result = await reader.handle(ModelProjectionReadRequest(topic=TOPIC))
    assert result.ok, result
    return {row["ticket"]: row for row in result.rows}


def test_done_items_exposure_declared() -> None:
    exposures = _topic_map()
    assert len(exposures) == 1
    exposure = exposures[TOPIC]
    assert exposure.topic == TOPIC
    assert exposure.bus_backed is True
    assert exposure.key_columns == ("ticket",)
    assert set(exposure.columns) == {
        "ticket",
        "done_at",
        "done_row_id",
        "lane",
        "pr",
        "closes",
        "projected_at",
    }


async def test_done_items_exposure_served(served: Served) -> None:
    writer, reader = served
    _feed(
        writer,
        [
            (V1, _v1(DONE1)),
            (V1, _v1(HANDED)),
            (V1, _v1(DONE_TWO)),
        ],
    )
    rows = await _read(reader)
    assert set(rows) == {"OMN-1", "OMN-2", "OMN-4"}
    assert rows["OMN-1"]["done_at"] == "2026-11-17T10:00:00Z"
    assert rows["OMN-1"]["done_row_id"] == work_ledger_row_id(DONE1)
    assert rows["OMN-1"]["lane"] == "alpha"
    assert rows["OMN-2"]["pr"] == "9"
    exposure = _topic_map()[TOPIC]
    for row in rows.values():
        assert set(row) == set(exposure.columns)
    await reader.close()


async def test_done_items_writer_replay(
    served: Served, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    writer, reader = served
    # A redelivery and a stale done event for OMN-1: in reverse order the
    # older DONE1 arrives last, so a writer that lets any done event replace
    # the held row ends with different rows.
    original_items = [
        (V1, _v1(DONE1)),
        (V1, _v1(DONE1_LATER)),
        (V1, _v1(DONE_TWO)),
    ] * 2
    _feed(writer, original_items)
    first = await _read(reader)
    assert first["OMN-1"]["done_at"] == "2026-11-17T11:00:00Z"

    db2 = tmp_path / "done-second.sqlite"
    overlay2 = tmp_path / "projection_binding-second.yaml"
    overlay2.write_text(
        f"kafka_bootstrap_servers: inmemory\ndatabase_url: 'sqlite:///{db2}'\n"
    )
    monkeypatch.setenv("OMNIMARKET_PROJECTION_RUNTIME_BINDING_OVERLAY", str(overlay2))
    writer2 = DoneItemsProjectionWriter(store=SqliteDoneItemsStore(db2))
    reader2 = HandlerProjectionRead(topic_map=_topic_map())
    _feed(writer2, list(reversed(original_items)))
    second = await _read(reader2)

    def without_projected(rows: Rows) -> Rows:
        return {
            ticket: {key: value for key, value in row.items() if key != "projected_at"}
            for ticket, row in rows.items()
        }

    assert without_projected(first) == without_projected(second)
    await reader.close()
    await reader2.close()


async def test_done_items_older_done_event_does_not_replace(
    served: Served,
) -> None:
    writer, reader = served
    _feed(
        writer,
        [
            (V1, _v1(DONE1_LATER)),
            (V1, _v1(DONE1)),
        ],
    )
    rows = await _read(reader)
    assert rows["OMN-1"]["done_at"] == "2026-11-17T11:00:00Z"
    assert rows["OMN-1"]["pr"] == "7"
    assert rows["OMN-1"]["closes"] == "OMN-9"
    await reader.close()


async def test_done_items_v2_payload_served(served: Served) -> None:
    writer, reader = served
    _feed(writer, [(V2, _v2(DONE1))])
    rows = await _read(reader)
    assert "OMN-1" in rows
    assert rows["OMN-1"]["done_row_id"] == work_ledger_row_id(DONE1)
    await reader.close()
