# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-resolved report to SQLite chain: drift, stale siblings and replay guards."""

from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.handlers.handler_local_environment_judgement import (
    load_local_alert_rules,
)
from omnimarket.nodes.node_projection_local_environment.models import (
    ModelLocalEnvironmentPlanRequest,
)
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter

pytestmark = pytest.mark.unit
_CONTRACT = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_projection_local_environment/contract.yaml"
)
_AT = datetime(2026, 10, 18, 6, tzinfo=UTC)
_TENANT = "6c24401e-0000-4000-8000-00000000000a"


def _contract() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(_CONTRACT.read_text())
    return loaded


def test_declared_output_state_and_terminal_agree() -> None:
    contract = _contract()
    terminal = "onex.evt.omnimarket.projection-local-environment-applied.v1"
    assert contract["event_bus"]["publish_topics"] == [terminal]
    assert contract["terminal_event"] == terminal


def test_declared_handler_materializes_reports_and_rejudges_sqlite_rows(
    tmp_path: Path,
) -> None:
    contract = _contract()
    handler = getattr(
        importlib.import_module(contract["handler"]["module"]),
        contract["handler"]["class"],
    )()
    module, name = contract["input_model"].rsplit(".", 1)
    model = getattr(importlib.import_module(module), name)
    rules = load_local_alert_rules()
    db = SqliteDatabaseAdapter(tmp_path / "environment.sqlite")
    exposure = contract["projection_api"]
    table = exposure["table"]
    key = ",".join(exposure["key_columns"])

    def apply(service: str, seconds: int) -> list[dict[str, object]]:
        report = model.model_validate(
            {
                "tenant_id": _TENANT,
                "mode": "compose",
                "service": service,
                "observed_state": "running",
                "reason": "healthy",
                "observed_at": _AT + timedelta(seconds=seconds),
                "versions": {"omnimarket": "0.4.317"},
            }
        )
        with db.write_transaction() as tx:
            writes: list[dict[str, object]] = list(
                handler.handle(
                    ModelLocalEnvironmentPlanRequest(
                        report=report,
                        stored_rows=tuple(tx.query(table, {"tenant_id": _TENANT})),
                        rules=rules,
                        updated_at=_AT,
                    )
                ).rows
            )
            for row in writes:
                tx.upsert(table, key, row)
        return writes

    assert len(apply("postgres", 0)) == 1
    assert len(apply("redpanda", 0)) == 1
    assert len(apply("redpanda", 75)) == 2
    rows = db.query(table, {"tenant_id": _TENANT}, order_by="service")
    assert [(r["service"], r["verdict"], r["alert_code"]) for r in rows] == [
        ("postgres", "stale", "HEALTH_UNKNOWN"),
        ("redpanda", "running", None),
    ]
    assert rows[0]["last_seen"] == _AT.isoformat()
    assert rows[1]["last_seen"] == (_AT + timedelta(seconds=75)).isoformat()
    assert apply("redpanda", 75) == []
    assert apply("redpanda", 30) == []
    assert db.query(table, {"tenant_id": _TENANT}, order_by="service") == rows
