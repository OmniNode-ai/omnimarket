# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23994: the host-overlay read binding resolves a DSN a real Postgres accepts.

The unit tests resolve the binding against SQLite. This one resolves it from a
node overlay row for the local host and connects to a real Postgres with the
resolved URL. SKIPS (never ERRORs) without ``INTEGRATION_POSTGRES_*`` settings
or a reachable database.
"""

from __future__ import annotations

import os
import socket
from pathlib import Path
from urllib.parse import quote_plus

import asyncpg
import pytest
import yaml

import omnimarket.handlers.projection_read_binding_host_overlay as host_overlay


def _real_dsn() -> str:
    password = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    if not password:
        pytest.skip("INTEGRATION_POSTGRES_PASSWORD not set")
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = os.environ.get("INTEGRATION_POSTGRES_PORT", "5432")
    user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
    db = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
    return f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{db}"


@pytest.mark.integration
async def test_host_overlay_binding_connects_to_real_postgres(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dsn = _real_dsn()
    host = socket.gethostname().split(".", 1)[0]
    overlay_dir = tmp_path / "root" / "node_projection_read_effect"
    overlay_dir.mkdir(parents=True)
    (overlay_dir / "overlay.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "projection_read_binding.v1",
                "bindings": [
                    {
                        "host": host,
                        "kafka_bootstrap_servers": "kafka:9092",
                        "kafka_consumer_group": "omn23994-integration",
                        "database_url_secret_ref": "env:OMN23994_REAL_DSN",
                    }
                ],
            }
        )
    )
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path / "root"))
    monkeypatch.setenv("OMN23994_REAL_DSN", dsn)
    monkeypatch.delenv("OMNIMARKET_PROJECTION_READ_BINDING_OVERLAY", raising=False)
    monkeypatch.delenv("OMNIMARKET_PROJECTION_RUNTIME_BINDING_OVERLAY", raising=False)

    binding = host_overlay.resolve_projection_read_binding()
    assert binding is not None
    assert binding.source.endswith(f"#{host}")
    try:
        conn = await asyncpg.connect(binding.resolve_database_url())
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"no reachable Postgres: {exc}")
    else:
        try:
            assert await conn.fetchval("SELECT 1") == 1
        finally:
            await conn.close()
