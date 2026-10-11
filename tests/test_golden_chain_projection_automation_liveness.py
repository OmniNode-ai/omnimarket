# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain for the automation-liveness projection (OMN-20802).

One hop per line of the node contract's ``golden_path``: the hops that read the
contract read it, the hops that move an event EXECUTE the real writer over a
SQLite file and read the rows back on another connection.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import yaml

from omnimarket.models.liveness.model_automation_liveness import (
    EnumAutomationLivenessEvent as Kind,
)
from omnimarket.nodes.node_projection_automation_liveness.handlers.handler_automation_liveness_writer import (
    AutomationLivenessProjectionWriter,
)
from omnimarket.nodes.node_projection_automation_liveness.ports.liveness_store import (
    SqliteLivenessStore,
)
from tests.helpers.automation_liveness_stream import (
    ACTIVE_HOST,
    ACTIVE_PROCESS,
    SILENT_HOST,
    SILENT_PROCESS,
    STREAM_ORDER,
    fixture_payload,
    fixture_topic,
)

pytestmark = pytest.mark.unit

CONTRACT_PATH = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_projection_automation_liveness/contract.yaml"
)


def _contract() -> dict[str, object]:
    loaded = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _rows(db_path: Path, sql: str) -> list[tuple[object, ...]]:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def test_hop_1_the_contract_subscribes_every_event_of_the_seam() -> None:
    event_bus = _contract()["event_bus"]
    assert isinstance(event_bus, dict)
    assert set(event_bus["subscribe_topics"]) == {fixture_topic(k) for k in Kind}
    assert event_bus["dlq_topics"] == [
        "onex.dlq.omnimarket.projection-automation-liveness-malformed.v1"
    ]


def test_hop_5_the_terminal_event_is_the_declared_applied_topic() -> None:
    assert (
        _contract()["terminal_event"]
        == "onex.evt.omnimarket.projection-automation-liveness-applied.v1"
    )


def test_hop_2_the_declaration_alone_writes_a_row_for_a_process_that_never_emitted(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "liveness.sqlite"
    writer = AutomationLivenessProjectionWriter(store=SqliteLivenessStore(db_path))
    result = writer.handle(
        {
            **fixture_payload(Kind.LIVENESS_DECLARED),
            "_topic": fixture_topic(Kind.LIVENESS_DECLARED),
            "_partition": 0,
            "_offset": 0,
        }
    )
    assert result["rows_upserted"] == 2
    rows = _rows(
        db_path,
        "SELECT process_id, host, process_state, last_run_at, verdict "
        "FROM automation_liveness_state ORDER BY process_id",
    )
    assert rows == [
        (ACTIVE_PROCESS, ACTIVE_HOST, "active", None, None),
        (SILENT_PROCESS, SILENT_HOST, "active", None, None),
    ]


def test_hops_3_and_4_the_stream_folds_into_rows_and_publishes_declared_snapshots(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "liveness.sqlite"
    writer = AutomationLivenessProjectionWriter(store=SqliteLivenessStore(db_path))
    for offset, kind in enumerate(STREAM_ORDER):
        out = writer.handle(
            {
                **fixture_payload(kind),
                "_topic": fixture_topic(kind),
                "_partition": 0,
                "_offset": offset,
            }
        )
        assert out["rows_upserted"] >= 1, f"{kind.value} wrote no row"
        relations = {row["relation"] for row in out["automation_liveness_rows"]}
        assert relations <= {
            "automation_liveness_state",
            "automation_run_history",
            "automation_alarm_episodes",
        }
    assert _rows(
        db_path,
        "SELECT verdict, last_outcome, open_episode_id "
        "FROM automation_liveness_state WHERE process_id = 'host-a/example-interval-job'",
    ) == [("missed", "ok", None)]
    projection_api = _contract()["projection_api"]
    assert isinstance(projection_api, dict)
    declared = {e["table"] for e in projection_api["exposures"]}
    assert declared == {
        "automation_liveness_state",
        "automation_run_history",
        "automation_alarm_episodes",
    }
