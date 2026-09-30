# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Real PostgreSQL proof for the standalone savings writer's split identity.

The legacy runner opens one pool. A tenant writer can insert a savings row, but
its watermark upsert is denied by omninode_internal. This test provisions the
two topology principals on a disposable cluster and exercises the runner's
actual adapters and watermark write. An adapter double cannot catch that ACL
failure or a tenant/internal DSN swap.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import quote

import asyncpg
import pytest

from omnimarket.nodes.node_projection_savings.handlers.handler_savings import (
    SavingsProjectionRunner,
)
from omnimarket.projection.runner import MessageMeta

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture
def postgres_lane(tmp_path: Path) -> Iterator[tuple[str, str]]:
    """Start an isolated native PG cluster; no shared lane or Docker state."""
    if not all(shutil.which(tool) for tool in ("initdb", "pg_ctl")):
        if os.environ.get("INTEGRATION_POSTGRES") == "1":
            pytest.fail("INTEGRATION_POSTGRES=1 requires native initdb and pg_ctl")
        pytest.skip("native initdb/pg_ctl unavailable for real-Postgres proof")
    data = tmp_path / "pgdata"
    # macOS limits the complete Unix socket path to 103 bytes; pytest's
    # per-test path can exceed that before PostgreSQL starts.
    sockets = Path(tempfile.mkdtemp(prefix="omn17454-", dir="/tmp"))
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    init = subprocess.run(
        [
            "initdb",
            "-D",
            str(data),
            "-U",
            "postgres",
            "--auth=trust",
            "--no-instructions",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if init.returncode:
        shutil.rmtree(sockets)
        if os.environ.get("INTEGRATION_POSTGRES") == "1":
            pytest.fail(f"isolated initdb unavailable: {init.stderr.strip()[-500:]}")
        pytest.skip(f"isolated initdb unavailable: {init.stderr.strip()[-500:]}")
    log = tmp_path / "postgres.log"
    start = subprocess.run(
        [
            "pg_ctl",
            "-D",
            str(data),
            "-l",
            str(log),
            "-o",
            f"-F -h '' -k {sockets} -p {port}",
            "-w",
            "start",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if start.returncode:
        detail = log.read_text(encoding="utf-8") if log.exists() else start.stderr
        shutil.rmtree(sockets)
        if os.environ.get("INTEGRATION_POSTGRES") == "1":
            pytest.fail(f"isolated pg_ctl unavailable: {detail.strip()[-500:]}")
        pytest.skip(f"isolated pg_ctl unavailable: {detail.strip()[-500:]}")
    admin_dsn = f"postgresql://postgres@/postgres?host={quote(str(sockets), safe='')}&port={port}"
    try:
        yield admin_dsn, f"?host={quote(str(sockets), safe='')}&port={port}"
    finally:
        stop = subprocess.run(
            ["pg_ctl", "-D", str(data), "-m", "immediate", "-w", "stop"],
            capture_output=True,
            text=True,
            check=False,
        )
        shutil.rmtree(sockets)
        assert stop.returncode == 0, stop.stderr


@pytest.mark.integration
async def test_savings_writer_uses_tenant_and_internal_principals(
    postgres_lane: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    admin_dsn, host_query = postgres_lane
    admin = await asyncpg.connect(admin_dsn)
    try:
        await admin.execute(
            "CREATE ROLE tenant_projection_writer LOGIN NOSUPERUSER NOBYPASSRLS"
        )
        await admin.execute(
            "CREATE ROLE omninode_runtime LOGIN NOSUPERUSER NOBYPASSRLS"
        )
        await admin.execute("CREATE SCHEMA omninode_internal")
        await admin.execute(
            "CREATE TABLE public.savings_estimates ("
            "id uuid PRIMARY KEY DEFAULT gen_random_uuid(), "
            "event_timestamp timestamptz NOT NULL, session_id text NOT NULL, "
            "model_local text NOT NULL, model_cloud_baseline text NOT NULL, "
            "local_cost_usd numeric NOT NULL, cloud_cost_usd numeric NOT NULL, "
            "savings_usd numeric NOT NULL, repo_name text, machine_id text, "
            "task_type text, prompt_tokens integer, completion_tokens integer, "
            "savings_method text, usage_source text, pricing_manifest_version text, "
            "tenant_id text NOT NULL, created_at timestamptz DEFAULT now(), "
            "updated_at timestamptz DEFAULT now(), "
            "UNIQUE (session_id, event_timestamp, model_local, model_cloud_baseline))"
        )
        await admin.execute(
            "CREATE TABLE omninode_internal.projection_watermarks ("
            "projection_name text PRIMARY KEY, last_offset bigint NOT NULL, "
            "events_projected bigint NOT NULL DEFAULT 0, "
            "last_projected_at timestamptz, updated_at timestamptz NOT NULL)"
        )
        await admin.execute(
            "CREATE TABLE omninode_internal.tenant_registry_mirror (tenant_slug text)"
        )
        await admin.execute(
            "GRANT CONNECT ON DATABASE postgres TO tenant_projection_writer, omninode_runtime"
        )
        await admin.execute("GRANT USAGE ON SCHEMA public TO tenant_projection_writer")
        await admin.execute(
            "GRANT INSERT, SELECT, UPDATE ON public.savings_estimates TO tenant_projection_writer"
        )
        await admin.execute(
            "GRANT USAGE ON SCHEMA omninode_internal TO omninode_runtime"
        )
        await admin.execute(
            "GRANT INSERT, SELECT, UPDATE ON omninode_internal.projection_watermarks "
            "TO omninode_runtime"
        )
        await admin.execute(
            "GRANT SELECT ON omninode_internal.tenant_registry_mirror TO omninode_runtime"
        )
    finally:
        await admin.close()

    tenant_dsn = f"postgresql://tenant_projection_writer@/postgres{host_query}"
    internal_dsn = f"postgresql://omninode_runtime@/postgres{host_query}"
    monkeypatch.setenv("KAFKA_BROKERS", "not-used.invalid:9092")
    monkeypatch.setenv("ONEX_DATABASE_TOPOLOGY_PROFILE", "local")
    monkeypatch.setenv("ONEX_TENANT_DB_URL", tenant_dsn)
    monkeypatch.setenv("OMNINODE_INTERNAL_DB_URL", internal_dsn)
    monkeypatch.setenv("OMNIDASH_ANALYTICS_DB_URL", tenant_dsn)

    runner = SavingsProjectionRunner()
    # The pre-change single-pool path has the correct tenant writer for the
    # savings row but cannot reach the internal watermark schema.
    await runner.db.connect()
    try:
        await runner._update_watermark("legacy:0", 1)
        assert (
            "Failed to update watermark: permission denied for schema omninode_internal"
            in caplog.text
        )
        caplog.clear()
    finally:
        await runner.db.close()
    await runner._connect_standalone_databases()
    try:
        tenant_db = runner.db_for("savings_estimates", operation="write")
        internal_db = runner.db_for("projection_watermarks", operation="write")
        assert (
            await tenant_db.fetchval("SELECT current_user")
            == "tenant_projection_writer"
        )
        assert await internal_db.fetchval("SELECT current_user") == "omninode_runtime"
        assert await runner._apply_event(
            "onex.evt.omnibase-infra.savings-estimated.v1",
            {
                "session_id": "binding-proof",
                "event_timestamp": "2026-09-27T00:00:00Z",
                "model_local": "local-model",
                "model_cloud_baseline": "cloud-model",
                "local_cost_usd": "0.25",
                "cloud_cost_usd": "1.00",
                "savings_usd": "0.75",
            },
            MessageMeta(
                topic="onex.evt.omnibase-infra.savings-estimated.v1",
                partition=0,
                offset=7,
                fallback_id="binding-proof",
            ),
            write_tenant="omninode",
        )
        await runner._update_watermark("proof:0", 7)
        assert (
            await tenant_db.fetchval(
                "SELECT savings_usd FROM public.savings_estimates WHERE session_id = 'binding-proof'"
            )
            == 0.75
        )
        assert (
            await internal_db.fetchval(
                "SELECT last_offset FROM omninode_internal.projection_watermarks "
                "WHERE projection_name = 'proof:0'"
            )
            == 7
        )
        receipt = {
            "tenant_principal": "tenant_projection_writer",
            "internal_principal": "omninode_runtime",
            "tenant_rows": await tenant_db.fetchval(
                "SELECT count(*) FROM public.savings_estimates"
            ),
            "watermark_offset": 7,
        }
        (tmp_path / "omn17454-binding-proof.json").write_text(
            json.dumps(receipt, sort_keys=True) + "\n", encoding="utf-8"
        )
    finally:
        await runner._close_standalone_databases()

    # Wrong-user DSNs must be rejected before the consumer can take a message.
    monkeypatch.setenv("ONEX_TENANT_DB_URL", internal_dsn)
    monkeypatch.setenv("OMNINODE_INTERNAL_DB_URL", tenant_dsn)
    wrong_user_runner = SavingsProjectionRunner()
    with pytest.raises(RuntimeError, match="expected principal 'omninode_runtime'"):
        await wrong_user_runner._connect_standalone_databases()

    # A legacy dashboard DSN cannot stand in for the missing tenant binding.
    monkeypatch.delenv("ONEX_TENANT_DB_URL")
    monkeypatch.delenv("OMNINODE_INTERNAL_DB_URL")
    legacy_only_runner = SavingsProjectionRunner()
    with pytest.raises(
        ValueError, match=r"omninode_runtime_service.*OMNINODE_INTERNAL_DB_URL"
    ):
        await legacy_only_runner._connect_standalone_databases()
