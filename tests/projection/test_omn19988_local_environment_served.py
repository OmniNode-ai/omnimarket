# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19988: local-environment.v1 is served from the local store as `onex dashboard` serves it.

Reports are stored the way the local writers store them, then read back through
the projection read node with the exposures built from the node contracts (no
hand-built topic map), so the contract declaration is what is under test.

Failure modes each test is written against:

* F6 the dashboard's own row (mode native) is not served, so the Overview panel
  and the API Keys card read "Not served yet";
* F7 another tenant's row is served to this tenant;
* F8 the dashboard token is served;
* a stopped compose service's verdict and alert are not what is served.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from functools import cache
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from omnimarket.handlers.handler_local_environment_judgement import (
    load_local_alert_rules,
)
from omnimarket.handlers.handler_local_environment_store import (
    apply_local_environment_status,
)
from omnimarket.models.model_local_environment_status import (
    ModelLocalEnvironmentStatus,
)
from omnimarket.nodes.node_local_dashboard_serve_effect.handlers.handler_local_dashboard_serve import (
    create_dashboard_app,
)
from omnimarket.nodes.node_local_dashboard_serve_effect.handlers.handler_native_environment_report import (
    build_native_report,
)
from omnimarket.nodes.node_projection_read_effect.handlers.handler_projection_read import (
    HandlerProjectionRead,
)
from omnimarket.nodes.node_projection_read_effect.ports.sqlite_row_source import (
    SqliteTableRowSource,
)
from omnimarket.projection.discovery import build_projection_topic_map
from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter

pytestmark = pytest.mark.unit

TOPIC = "onex.snapshot.projection.local-environment.v1"
TENANT = "6c24401e-0000-4000-8000-00000000000a"
OTHER_TENANT = "11111111-2222-4333-8444-555555555555"
TOKEN = "never-served-token-value"
T0 = datetime(2026, 10, 18, 6, 0, tzinfo=UTC)


@cache
def _topics() -> dict[str, ProjectionTableConfig]:
    return build_projection_topic_map()


def _native(db_path: Path, tenant: str, at: datetime) -> ModelLocalEnvironmentStatus:
    return build_native_report(
        tenant_id=tenant,
        store_location=db_path,
        token_minted_at=at,
        observed_at=at,
        exposure_count=len(_topics()),
        versions={"omnimarket": "0.4.317", "omnibase_core": "0.47.46"},
    )


def _compose(
    tenant: str, service: str, state: str, at: datetime
) -> ModelLocalEnvironmentStatus:
    return ModelLocalEnvironmentStatus.model_validate(
        {
            "tenant_id": tenant,
            "mode": "compose",
            "service": service,
            "observed_state": state,
            "reason": f"{service} is {state}",
            "observed_at": at.isoformat(),
        }
    )


def _store(db_path: Path, reports: list[ModelLocalEnvironmentStatus]) -> Path:
    adapter = SqliteDatabaseAdapter(db_path)
    rules = load_local_alert_rules()
    for report in reports:
        apply_local_environment_status(report, adapter, rules)
    return db_path


def _read(db_path: Path, tenant: str) -> dict[str, Any]:
    topics = _topics()
    handler = HandlerProjectionRead(
        topic_map=topics, row_source=SqliteTableRowSource(db_path)
    )
    client = TestClient(
        create_dashboard_app(handler=handler, topic_map=topics, tenant=tenant)
    )
    response = client.get(f"/projection/{TOPIC}")
    assert response.status_code == 200, response.json()
    body: dict[str, Any] = response.json()
    return body


def test_the_dashboards_own_native_row_is_served_with_its_access_fields(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "delegation.sqlite"
    _store(db_path, [_native(db_path, TENANT, T0)])

    rows = _read(db_path, TENANT)["rows"]

    assert [(r["mode"], r["service"]) for r in rows] == [("native", "local-runtime")]
    row = rows[0]
    assert row["verdict"] == "running"
    assert row["access_mode"] == "loopback-token"
    assert row["token_minted_at"]
    assert "Restart" in row["rotate_hint"]
    assert row["versions"]["omnimarket"] == "0.4.317"
    assert row["store_kind"] == "sqlite"


def test_another_tenants_rows_are_never_served(tmp_path: Path) -> None:
    db_path = tmp_path / "delegation.sqlite"
    _store(
        db_path,
        [
            _native(db_path, TENANT, T0),
            _native(db_path, OTHER_TENANT, T0),
            _compose(OTHER_TENANT, "redpanda", "down", T0),
        ],
    )

    mine = _read(db_path, TENANT)["rows"]
    theirs = _read(db_path, OTHER_TENANT)["rows"]

    assert {r["tenant_id"] for r in mine} == {TENANT}
    assert len(mine) == 1
    assert {r["tenant_id"] for r in theirs} == {OTHER_TENANT}
    assert {r["service"] for r in theirs} == {"local-runtime", "redpanda"}


def test_the_token_value_is_never_in_a_served_row(tmp_path: Path) -> None:
    db_path = tmp_path / "delegation.sqlite"
    _store(db_path, [_native(db_path, TENANT, T0)])
    body = _read(db_path, TENANT)
    assert TOKEN not in str(body)
    for row in body["rows"]:
        assert not [k for k in row if "token" in k and k != "token_minted_at"]


def test_a_stopped_compose_service_is_served_down_with_its_alert(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "delegation.sqlite"
    _store(
        db_path,
        [
            _compose(TENANT, "postgres", "running", T0),
            _compose(TENANT, "redpanda", "running", T0),
            _compose(TENANT, "redpanda", "down", T0 + timedelta(seconds=15)),
        ],
    )
    rows = {r["service"]: r for r in _read(db_path, TENANT)["rows"]}
    assert (rows["redpanda"]["verdict"], rows["redpanda"]["alert_code"]) == (
        "down",
        "SERVICE_DOWN",
    )
    assert rows["postgres"]["verdict"] == "running"
    assert rows["postgres"]["alert_code"] is None
