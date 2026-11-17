# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden-chain tests for node_projection_done_items.

Catches: declared topic hops and ACL grants drift; a terminal done row not
reaching the done_items table; malformed payloads writing rows; and merged
STATUS rows being incorrectly treated as done.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.events.model_ledger_row_event import (
    work_ledger_event_id,
    work_ledger_row_id,
)
from omnimarket.nodes.node_projection_done_items.handlers.handler_done_items_writer import (
    DoneItemsProjectionWriter,
    done_items_from_payload,
)
from omnimarket.nodes.node_projection_done_items.ports.done_items_store import (
    SqliteDoneItemsStore,
)
from omnimarket.nodes.node_work_ledger_emit_effect.handlers.row_parser import (
    LedgerRowRefusalError,
    parse_ledger_row,
)

pytestmark = pytest.mark.unit

CONTRACT_PATH = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_projection_done_items/contract.yaml"
)

TERMINAL_V1 = "onex.evt.omnimarket.work-ledger-terminal.v1"  # onex-topic-allow: declared subscribe topic
TERMINAL_V2 = "onex.evt.omnimarket.work-ledger-terminal.v2"  # onex-topic-allow: declared subscribe topic
SNAPSHOT_TOPIC = (
    "onex.snapshot.projection.done-items.v1"  # onex-topic-allow: declared publish topic
)
MALFORMED_DLQ = "onex.dlq.omnimarket.projection-done-items-malformed.v1"  # onex-topic-allow: declared dlq topic


def _producer_payload(raw_row: str) -> dict[str, object]:
    # Built exactly as node_work_ledger_emit_effect handler_work_ledger_emit.py emit_event.
    event = parse_ledger_row(raw_row)
    return {
        **event.model_dump(mode="json"),
        "event_id": str(work_ledger_event_id(event.ledger_id, event.row_id)),
    }


def test_golden_chain_hops_are_the_declared_topics() -> None:
    contract = yaml.safe_load(CONTRACT_PATH.read_text())
    event_bus = contract["event_bus"]

    assert event_bus["subscribe_topics"] == [TERMINAL_V1, TERMINAL_V2]
    assert event_bus["publish_topics"] == [SNAPSHOT_TOPIC]
    assert event_bus["dlq_topics"] == [MALFORMED_DLQ]

    exposures = contract["projection_api"]["exposures"]
    assert exposures[0]["topic"] == SNAPSHOT_TOPIC
    assert exposures[0]["bus_backed"] is True

    handlers = contract["handler_routing"]["handlers"]
    names = [entry["handler"]["name"] for entry in handlers]
    assert names == ["DoneItemsProjectionWriter"]
    assert DoneItemsProjectionWriter.onex_runtime_inprocess_dispatch is True

    def operations_for(topic: str) -> list[str]:
        operations: list[str] = []
        for principal in contract["broker_acl_grants"]:
            for grant in principal.get("grants", []):
                if grant.get("name") == topic:
                    operations.append(grant.get("operation"))
        return operations

    assert "WRITE" in operations_for(SNAPSHOT_TOPIC)
    for terminal_topic in (TERMINAL_V1, TERMINAL_V2):
        assert "READ" in operations_for(terminal_topic)


def test_golden_chain_done_row_reaches_the_table(tmp_path: Path) -> None:
    db_path = tmp_path / "done_items.db"
    store = SqliteDoneItemsStore(db_path)
    writer = DoneItemsProjectionWriter(store=store)

    raw_row = "2026-11-17T10:00:00Z | TERMINAL | lane=alpha | ticket=OMN-1 | outcome=done | running_path=onex x | finished"
    payload = _producer_payload(raw_row)
    result = writer.handle(
        {**payload, "_topic": TERMINAL_V1, "_partition": 0, "_offset": 1}
    )

    assert result["rows_upserted"] == 1

    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            "SELECT ticket, done_at, done_row_id, lane FROM done_items"
        ).fetchall()

    assert rows == [
        ("OMN-1", "2026-11-17T10:00:00Z", work_ledger_row_id(raw_row), "alpha")
    ]


def test_golden_chain_error_leg_malformed_payload_writes_nothing(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "done_items.db"
    store = SqliteDoneItemsStore(db_path)
    writer = DoneItemsProjectionWriter(store=store)

    raw_row = "2026-11-17T10:00:00Z | TERMINAL | lane=alpha | ticket=OMN-1 | outcome=done | running_path=onex x | finished"
    valid_payload = _producer_payload(raw_row)
    # The producer's own payload with its ledger row garbled in transit.
    garbled = valid_payload | {"raw_row": "not a ledger row"}
    with pytest.raises((ValueError, ValidationError, LedgerRowRefusalError)):
        writer.handle({**garbled, "_topic": TERMINAL_V1, "_partition": 0, "_offset": 1})

    result = writer.handle(
        {**valid_payload, "_topic": TERMINAL_V1, "_partition": 0, "_offset": 2}
    )

    assert result["rows_upserted"] == 1
    with sqlite3.connect(db_path) as connection:
        count = connection.execute("SELECT COUNT(*) FROM done_items").fetchone()[0]
    assert count == 1


def test_golden_chain_merged_status_is_not_done() -> None:
    raw_row = "2026-11-17T09:00:00Z | STATUS | lane=alpha | ticket=OMN-2 | repo=omnimarket | pr=5 | state=merged"
    assert done_items_from_payload(_producer_payload(raw_row)) == ()
