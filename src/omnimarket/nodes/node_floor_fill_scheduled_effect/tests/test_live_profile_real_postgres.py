# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A due live tick against a real Postgres table leaves the fill's state row in it.

The table is the shape of omnibase_infra's migration 113, made in a schema of the test's own (the fixture of the store
test). The test skips, never errors, where no database is reachable.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_floor_fill_orchestrator.tests.test_floor_fill_postgres_store import (
    schema_dsn,  # noqa: F401 -- a pytest fixture, used by name
)
from omnimarket.nodes.node_floor_fill_scheduled_effect.handlers.handler_floor_fill_scheduled import (
    HandlerFloorFillScheduled,
)
from omnimarket.nodes.node_floor_fill_scheduled_effect.tests.test_live_profile import (
    EXAMPLE,
    NODE,
    T0,
    tick,
)

psycopg2: Any = importlib.import_module("psycopg2")
sql: Any = importlib.import_module("psycopg2.sql")

pytestmark = pytest.mark.integration


def test_ac4_a_live_tick_leaves_a_row_in_the_floor_fill_state_table(
    tmp_path: Path,
    schema_dsn: tuple[str, str],  # noqa: F811 -- the fixture's parameter name
) -> None:
    dsn, schema_name = schema_dsn
    (tmp_path / NODE).mkdir()
    (tmp_path / NODE / "overlay.yaml").write_text(
        yaml.safe_dump(
            {
                "job_id": "floor_fill",
                "run_interval_seconds": 300,
                "grace_seconds": 60,
                "fill_overlay_id": "node_floor_fill_orchestrator",
                "profile": "live",
            }
        ),
        encoding="utf-8",
    )
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["floor_fill_live"]["store_schema_name"] = schema_name
    (tmp_path / "node_floor_fill_orchestrator").mkdir()
    (tmp_path / "node_floor_fill_orchestrator" / "overlay.yaml").write_text(
        yaml.safe_dump(document), encoding="utf-8"
    )
    env = {
        "ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path),
        document["floor_fill_live"]["store_dsn_env"]: dsn,
    }

    event = HandlerFloorFillScheduled(environ=env).handle(tick(T0))

    assert event is not None
    assert "store-dsn-unset" not in event.detail
    assert "store-unreachable" not in event.detail
    connection = psycopg2.connect(dsn, connect_timeout=5)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                sql.SQL("SELECT count(*) FROM {relation}").format(
                    relation=sql.Identifier(schema_name, "floor_fill_state")
                )
            )
            assert cursor.fetchone()[0] >= 1
    finally:
        connection.close()
