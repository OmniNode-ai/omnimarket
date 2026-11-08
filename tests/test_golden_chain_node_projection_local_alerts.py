# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-resolved transition to SQLite chain: one row per key, replays and late transitions."""

from __future__ import annotations

import importlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_projection_local_alerts.models import (
    ModelLocalAlertsPlanRequest,
)
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter

pytestmark = pytest.mark.unit
_CONTRACT = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_projection_local_alerts/contract.yaml"
)
_AT = datetime(2026, 11, 6, 9, tzinfo=UTC)
_TENANT = "6c24401e-0000-4000-8000-00000000000a"
_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS local_alerts (
    tenant_id TEXT NOT NULL,
    code TEXT NOT NULL,
    subject TEXT NOT NULL,
    mode TEXT NOT NULL,
    service TEXT NOT NULL,
    state TEXT NOT NULL,
    severity TEXT NOT NULL,
    raised_at TEXT NOT NULL,
    cleared_at TEXT,
    evidence TEXT NOT NULL,
    last_transition_id TEXT NOT NULL,
    last_transition_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, code, subject)
)
"""


def _contract() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(_CONTRACT.read_text())
    return loaded


def test_declared_output_state_and_terminal_agree() -> None:
    contract = _contract()
    terminal = "onex.evt.omnimarket.projection-local-alerts-applied.v1"
    assert contract["event_bus"]["publish_topics"] == [terminal]
    assert contract["terminal_event"] == terminal


def test_declared_handler_folds_transitions_into_one_row_per_key(
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
                "tenant_id": _TENANT,
                "code": "SERVICE_DOWN",
                "subject": "compose/postgres",
                "transition": direction,
                "severity": "critical",
                "raised_at": _AT + timedelta(seconds=raised),
                "occurred_at": _AT + timedelta(seconds=occurred),
                "evidence": {
                    "mode": "compose",
                    "service": "postgres",
                    "verdict": "down" if direction == "raised" else "running",
                    "reason": "postgres probe",
                    "last_seen": _AT + timedelta(seconds=occurred),
                },
            }
        )
        with db.write_transaction() as tx:
            stored = tx.query(
                table,
                {
                    "tenant_id": _TENANT,
                    "code": "SERVICE_DOWN",
                    "subject": "compose/postgres",
                },
            )
            row: dict[str, object] | None = handler.handle(
                ModelLocalAlertsPlanRequest(
                    transition=transition, stored_row=stored[0] if stored else None
                )
            ).row
            if row is not None:
                tx.upsert(table, key, row)
        return row

    assert apply("raised", 0, 0) is not None
    assert apply("cleared", 0, 30) is not None
    assert apply("raised", 60, 60) is not None
    rows = db.query(table, {"tenant_id": _TENANT})
    assert len(rows) == 1
    assert (rows[0]["state"], rows[0]["cleared_at"]) == ("raised", None)
    assert json.loads(str(rows[0]["evidence"]))["verdict"] == "down"
    # A replay and a late transition plan nothing.
    assert apply("raised", 60, 60) is None
    assert apply("cleared", 0, 30) is None
    assert db.query(table, {"tenant_id": _TENANT}) == rows
