# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-resolved transition to SQLite chain: one row per alert, replays and late transitions."""

from __future__ import annotations

import importlib
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_projection_lab_alerts.models.model_lab_alerts_plan import (
    ModelLabAlertsPlanRequest,
)
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter

pytestmark = pytest.mark.unit
_CONTRACT = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_projection_lab_alerts/contract.yaml"
)
_AT = datetime(2026, 11, 24, 9, tzinfo=UTC)
_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS lab_alerts (
    alert_id TEXT NOT NULL PRIMARY KEY,
    source TEXT NOT NULL,
    subject TEXT NOT NULL,
    state TEXT NOT NULL,
    severity TEXT NOT NULL,
    owner TEXT NOT NULL,
    detail TEXT NOT NULL,
    raised_at TEXT NOT NULL,
    cleared_at TEXT,
    last_transition_id TEXT NOT NULL,
    last_transition_at TEXT NOT NULL
)
"""


def _contract() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(_CONTRACT.read_text())
    return loaded


def test_declared_output_state_and_terminal_agree() -> None:
    contract = _contract()
    terminal = "onex.evt.omnimarket.projection-lab-alerts-applied.v1"
    assert contract["event_bus"]["publish_topics"] == [terminal]
    assert contract["terminal_event"] == terminal


def test_declared_handler_folds_transitions_into_one_row_per_alert(
    tmp_path: Path,
) -> None:
    contract = _contract()
    handler = getattr(
        importlib.import_module(contract["handler"]["module"]),
        contract["handler"]["class"],
    )()
    module, name = contract["input_model"].rsplit(".", 1)
    model = getattr(importlib.import_module(module), name)
    path = tmp_path / "alerts.sqlite"
    with sqlite3.connect(path) as conn:
        conn.executescript(_TABLE_SQL)
    db = SqliteDatabaseAdapter(path)
    exposure = contract["projection_api"]
    table = exposure["table"]
    key = ",".join(exposure["key_columns"])

    def apply(direction: str, raised: int, occurred: int) -> dict[str, object] | None:
        transition = model.model_validate(
            {
                "source": "dev_tag_heartbeat",
                "subject": "lane-dev",
                "transition": direction,
                "severity": "SEV2",
                "owner": "lab-floor",
                "detail": "dev tag has not moved",
                "raised_at": _AT + timedelta(seconds=raised),
                "occurred_at": _AT + timedelta(seconds=occurred),
            }
        )
        with db.write_transaction() as tx:
            stored = tx.query(table, {"alert_id": transition.alert_id})
            row: dict[str, object] | None = handler.handle(
                ModelLabAlertsPlanRequest(
                    transition=transition, stored_row=stored[0] if stored else None
                )
            ).row
            if row is not None:
                tx.upsert(table, key, row)
        return row

    assert apply("raised", 0, 0) is not None
    assert apply("cleared", 0, 30) is not None
    assert apply("raised", 60, 60) is not None
    rows = db.query(table, {"source": "dev_tag_heartbeat"})
    assert len(rows) == 1
    assert (rows[0]["state"], rows[0]["cleared_at"]) == ("open", None)
    # A replay and a late transition plan nothing.
    assert apply("raised", 60, 60) is None
    assert apply("cleared", 0, 30) is None
    assert db.query(table, {"source": "dev_tag_heartbeat"}) == rows
