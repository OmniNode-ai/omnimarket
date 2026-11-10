# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19986: local-identity.v1 is served from the local store as `onex dashboard` serves it.

The row is stored the way `onex local init` stores it, then read back through
the projection read node with the exposures built from the node contracts, so
the contract declaration is what is under test.

Failure modes each test is written against:

* F8 the served row differs from the stored row (tenant or mint time);
* another tenant's identity row is served to this tenant;
* the catalogue does not list the exposure, so the page reads "not served".
"""

from __future__ import annotations

import sqlite3
from functools import cache
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from omnimarket.local_deployment.tenant_identity import (
    mint_local_tenant_identity,
    reset_local_tenant_identity_cache,
)
from omnimarket.nodes.node_local_dashboard_serve_effect.handlers.handler_local_dashboard_serve import (
    create_dashboard_app,
)
from omnimarket.nodes.node_projection_read_effect.handlers.handler_projection_read import (
    HandlerProjectionRead,
)
from omnimarket.nodes.node_projection_read_effect.ports.sqlite_row_source import (
    SqliteTableRowSource,
)
from omnimarket.projection.discovery import build_projection_topic_map
from omnimarket.projection.models import ProjectionTableConfig

pytestmark = pytest.mark.unit

TOPIC = "onex.snapshot.projection.local-identity.v1"
TENANT = UUID("6c24401e-0000-4000-8000-00000000000a")
OTHER_TENANT = UUID("11111111-2222-4333-8444-555555555555")


@pytest.fixture(autouse=True)
def _fresh_identity_cache() -> None:
    reset_local_tenant_identity_cache()


@cache
def _topics() -> dict[str, ProjectionTableConfig]:
    return build_projection_topic_map()


def _client(db_path: Path, tenant: UUID) -> TestClient:
    topics = _topics()
    handler = HandlerProjectionRead(
        topic_map=topics, row_source=SqliteTableRowSource(db_path)
    )
    return TestClient(
        create_dashboard_app(handler=handler, topic_map=topics, tenant=str(tenant))
    )


def _stored(db_path: Path) -> list[dict[str, Any]]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM local_identity")]
    finally:
        conn.close()


def test_the_served_identity_row_is_the_stored_row(tmp_path: Path) -> None:
    db_path = tmp_path / "delegation.sqlite"
    identity = mint_local_tenant_identity(db_path=db_path, tenant_uuid=TENANT)

    response = _client(db_path, TENANT).get(f"/projection/{TOPIC}")

    assert response.status_code == 200, response.json()
    rows = response.json()["rows"]
    stored = _stored(db_path)
    assert len(rows) == len(stored) == 1
    assert rows[0]["tenant_id"] == stored[0]["tenant_id"] == str(TENANT)
    assert rows[0]["minted_at"] == stored[0]["minted_at"]
    assert rows[0]["minted_at"] == identity.recorded_at.isoformat()


def test_another_tenants_identity_row_is_never_served(tmp_path: Path) -> None:
    db_path = tmp_path / "delegation.sqlite"
    mint_local_tenant_identity(db_path=db_path, tenant_uuid=TENANT)
    other = tmp_path / "other.sqlite"
    mint_local_tenant_identity(db_path=other, tenant_uuid=OTHER_TENANT)

    mine = _client(db_path, TENANT).get(f"/projection/{TOPIC}")
    assert {r["tenant_id"] for r in mine.json()["rows"]} == {str(TENANT)}

    refused = _client(db_path, TENANT).get(
        f"/projection/{TOPIC}", params={"tenant": str(OTHER_TENANT)}
    )
    assert refused.status_code == 422
    assert "rows" not in refused.json() or refused.json()["rows"] == []


def test_the_catalogue_lists_the_identity_exposure(tmp_path: Path) -> None:
    db_path = tmp_path / "delegation.sqlite"
    mint_local_tenant_identity(db_path=db_path, tenant_uuid=TENANT)

    catalogue = _client(db_path, TENANT).get("/projections").json()

    listed = {entry["topic"]: entry for entry in catalogue["topics"]}
    assert TOPIC in listed
    assert catalogue["tenant"] == str(TENANT)
