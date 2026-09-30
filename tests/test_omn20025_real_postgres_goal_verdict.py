# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC1 proof: typed goal ingress reaches a revision-bound PostgreSQL row.

This test uses the contract-resolved LocalRuntimeDispatch transport for the
producer and the production projection writer's public ``handle`` entrypoint
with its documented runtime metadata and a real isolated PostgreSQL binding.
It proves the composed producer payload and durable write; it does not claim
to boot the deployed Kafka runtime.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, quote_plus
from uuid import uuid4

import asyncpg
import pytest
import yaml

from omnimarket.adapters.codex.local_runtime_dispatch import LocalRuntimeDispatch
from omnimarket.adapters.codex.runtime_client import ModelDispatchBusCommand
from omnimarket.nodes.node_projection_dod_verdict.handlers.handler_dod_verdict_runner import (
    DodVerdictProjectionWriter,
)

_ROOT = Path(__file__).resolve().parents[1]
_MIGRATIONS = _ROOT / "src/omnimarket/nodes/node_projection_dod_verdict/migrations"
_DOCKER = shutil.which("docker")
_POSTGRES_BACKEND_ENV = "OMN20025_POSTGRES_BACKEND"
_ADAPTER_TOPIC = "onex.cmd.codex.pattern-b-dispatch.v1"
_RESPONSE_TOPIC = "onex.evt.codex.pattern-b-dispatch-completed.v1"
_VERIFY_COMMAND = "onex.cmd.omnimarket.dod-verify-start.v1"
_VERIFY_TERMINAL = "onex.evt.omnimarket.dod-verify-completed.v1"


@dataclass(frozen=True)
class _PostgresBinding:
    admin_dsn: str
    runtime_dsn: str
    database: str
    host: str
    port: int
    server_version: str


def _dsn(
    host: str,
    port: int,
    database: str,
    *,
    user: str,
    password: str | None = None,
) -> str:
    auth = quote(user)
    if password is not None:
        auth = f"{auth}:{quote(password)}"
    return f"postgresql://{auth}@{host}:{port}/{quote_plus(database)}"


async def _accepts_sql(dsn: str) -> bool:
    try:
        connection = await asyncpg.connect(dsn, timeout=1)
    except (OSError, asyncpg.PostgresError):
        return False
    await connection.close()
    return True


@pytest.fixture(scope="module")
def isolated_postgres_url() -> Iterator[_PostgresBinding]:
    """Use an explicitly configured PG16 backend and unique test database."""
    backend = os.environ.get(_POSTGRES_BACKEND_ENV)
    if backend not in {"ci", "docker"}:
        if os.environ.get("CI", "").lower() == "true":
            pytest.fail(
                f"CI must select {_POSTGRES_BACKEND_ENV}=ci for the required "
                "PostgreSQL 16 integration proof"
            )
        pytest.skip(
            f"set {_POSTGRES_BACKEND_ENV}=ci or docker explicitly; "
            "no PostgreSQL backend is selected, so this real-DB proof is not run"
        )
    container_name = f"omn20025-gc2-pg-{uuid4().hex[:12]}"
    database = f"gc2_{uuid4().hex[:10]}"
    container_id: str | None = None
    runtime_password = uuid4().hex
    try:
        host: str
        port: int
        admin_user: str
        admin_password: str | None

        if backend == "ci":
            required = (
                "INTEGRATION_POSTGRES_HOST",
                "INTEGRATION_POSTGRES_PORT",
                "INTEGRATION_POSTGRES_USER",
                "INTEGRATION_POSTGRES_PASSWORD",
                "INTEGRATION_POSTGRES_DB",
            )
            missing = [name for name in required if not os.environ.get(name)]
            if missing:
                pytest.fail(
                    "explicit CI PostgreSQL backend is missing configured inputs: "
                    + ", ".join(missing)
                )
            host = os.environ["INTEGRATION_POSTGRES_HOST"]
            port = int(os.environ["INTEGRATION_POSTGRES_PORT"])
            admin_user = os.environ["INTEGRATION_POSTGRES_USER"]
            admin_password = os.environ["INTEGRATION_POSTGRES_PASSWORD"]
        else:
            if _DOCKER is None:
                pytest.skip("docker backend selected but docker client is unavailable")
            docker_ready = (
                subprocess.run(
                    [_DOCKER, "info", "--format", "{{.ServerVersion}}"],
                    check=False,
                    capture_output=True,
                    text=True,
                ).returncode
                == 0
            )
            if not docker_ready:
                pytest.skip("docker backend selected but Docker daemon is unavailable")
            assert _DOCKER is not None
            try:
                created = subprocess.run(
                    [
                        _DOCKER,
                        "run",
                        "--detach",
                        "--rm",
                        "--name",
                        container_name,
                        "--label",
                        "omnimarket.omn20025=isolated-test",
                        "--env",
                        "POSTGRES_HOST_AUTH_METHOD=trust",
                        "--env",
                        f"POSTGRES_DB={database}",
                        "--publish",
                        "127.0.0.1::5432",
                        "postgres:16-alpine",
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                container_id = created.stdout.strip()
                published = subprocess.run(
                    [_DOCKER, "port", container_id, "5432/tcp"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
                host, port_text = published.splitlines()[0].rsplit(":", maxsplit=1)
                port = int(port_text)
                admin_user = "postgres"
                admin_password = None
            except (OSError, subprocess.CalledProcessError, ValueError) as exc:
                if container_id is not None:
                    subprocess.run(
                        [_DOCKER, "rm", "--force", container_id],
                        check=False,
                        capture_output=True,
                    )
                    container_id = None
                pytest.fail(f"explicit Docker PostgreSQL 16 startup failed: {exc}")

        admin_database = (
            "postgres" if backend == "docker" else os.environ["INTEGRATION_POSTGRES_DB"]
        )
        admin_dsn = _dsn(
            host,
            port,
            admin_database,
            user=admin_user,
            password=admin_password,
        )

        server_version: str | None = None
        for _ in range(150):
            if asyncio.run(_accepts_sql(admin_dsn)):

                async def read_server_version() -> str:
                    connection = await asyncpg.connect(admin_dsn)
                    try:
                        return str(await connection.fetchval("SELECT version()"))
                    finally:
                        await connection.close()

                server_version = asyncio.run(read_server_version())
                break
            time.sleep(0.1)
        else:
            pytest.fail(
                "owned isolated PostgreSQL endpoint did not accept a connection"
            )
        assert server_version is not None
        assert "PostgreSQL 16." in server_version, server_version

        async def provision_database() -> None:
            connection = await asyncpg.connect(admin_dsn)
            try:
                await connection.execute(f"CREATE DATABASE {database}")
                await connection.execute(
                    """DO $$ BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_roles WHERE rolname = 'omninode_runtime'
                    ) THEN
                        CREATE ROLE omninode_runtime LOGIN;
                    END IF;
                    END $$"""
                )
                await connection.execute(
                    f"ALTER ROLE omninode_runtime WITH LOGIN PASSWORD '{runtime_password}'"
                )
            finally:
                await connection.close()

        asyncio.run(provision_database())
        runtime_dsn = _dsn(
            host,
            port,
            database,
            user="omninode_runtime",
            password=runtime_password,
        )
        print(f"OMN20025 isolated PostgreSQL backend={backend}; {server_version}")

        yield _PostgresBinding(
            admin_dsn=admin_dsn,
            runtime_dsn=runtime_dsn,
            database=database,
            host=host,
            port=port,
            server_version=server_version,
        )
    finally:
        if container_id is not None:
            subprocess.run(
                [_DOCKER, "rm", "--force", container_id],
                check=False,
                capture_output=True,
            )
        if backend == "ci" and "admin_dsn" in locals():

            async def drop_database() -> None:
                connection = await asyncpg.connect(admin_dsn)
                try:
                    await connection.execute(
                        "SELECT pg_terminate_backend(pid) "
                        "FROM pg_stat_activity WHERE datname = $1 "
                        "AND pid <> pg_backend_pid()",
                        database,
                    )
                    await connection.execute(f"DROP DATABASE IF EXISTS {database}")
                finally:
                    await connection.close()

            with contextlib.suppress(OSError, asyncpg.PostgresError):
                asyncio.run(drop_database())


def _apply_migrations(dsn: str) -> None:
    async def apply() -> None:
        connection = await asyncpg.connect(dsn)
        try:
            await connection.execute("CREATE SCHEMA omninode_internal")
            for filename in (
                "0000_create_dod_verify_runs.sql",
                "0001_grant_omninode_runtime_dod_verify_runs.sql",
                "0002_dod_verify_runs_delegation_correlation_id.sql",
                "0003_dod_verify_runs_goal.sql",
            ):
                await connection.execute(
                    (_MIGRATIONS / filename).read_text(encoding="utf-8")
                )
        finally:
            await connection.close()

    asyncio.run(apply())


@pytest.mark.integration
def test_runtime_goal_payload_is_stored_with_its_exact_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_postgres_url: _PostgresBinding,
) -> None:
    """A goal without a ticket file produces a row with exact lineage IDs."""
    binding = isolated_postgres_url
    _apply_migrations(binding.admin_dsn)

    workspace = tmp_path / "evidence-root"
    workspace.mkdir()
    (workspace / "proof.txt").write_text("inline goal check\n", encoding="utf-8")
    monkeypatch.setenv("OMNI_HOME", str(workspace))

    binding_path = tmp_path / "projection-binding.yaml"
    binding_path.write_text(
        yaml.safe_dump(
            {
                "kafka_bootstrap_servers": "127.0.0.1:9092",
                "kafka_consumer_group": "gc2-isolated-test",
                "kafka_client_id": "gc2-isolated-test",
                "database_url": binding.runtime_dsn,
                "source": "isolated-om20025-test",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(
        "OMNIMARKET_PROJECTION_RUNTIME_BINDING_OVERLAY", str(binding_path)
    )

    goal_id = uuid4()
    parent_goal_id = uuid4()
    contract_revision = uuid4()
    correlation_id = uuid4()
    command_payload: dict[str, object] = {
        "ticket_id": "OMN-20025",
        "goal_id": str(goal_id),
        "parent_goal_id": str(parent_goal_id),
        "level": "workflow_lane",
        "contract_revision": str(contract_revision),
        "contract_schema_version": "1.0.0",
        "execution_audience": "local_done_gate",
        "dod_evidence": [
            {
                "id": "gc2-inline-proof",
                "description": "An inline goal check with no ticket contract file.",
                "checks": [{"check_type": "file_exists", "check_value": "proof.txt"}],
            }
        ],
    }
    runtime = LocalRuntimeDispatch(
        adapter_command_topic=_ADAPTER_TOPIC,
        state_root=tmp_path / "local-runtime-state",
    )
    command = ModelDispatchBusCommand(
        command_name="dod-verify",
        requester="omn20025-ac1-test",
        payload=command_payload,
        correlation_id=correlation_id,
        response_topic=_RESPONSE_TOPIC,
        timeout_seconds=30.0,
    )
    terminal, evidence = asyncio.run(runtime.dispatch(command))

    assert evidence.runtime_observation.status == "OBSERVED"
    assert evidence.node_name == "node_dod_verify"
    assert evidence.command_topic == _VERIFY_COMMAND
    assert terminal.status == "completed"
    assert terminal.payload is not None
    producer_payload = dict(terminal.payload)
    assert producer_payload["goal_id"] == str(goal_id)
    assert producer_payload["parent_goal_id"] == str(parent_goal_id)
    assert producer_payload["level"] == "workflow_lane"
    assert producer_payload["contract_revision"] == str(contract_revision)
    assert producer_payload["total_checks"] == 1

    writer = DodVerdictProjectionWriter()
    writer_payload = {
        **producer_payload,
        "_topic": _VERIFY_TERMINAL,
        "_partition": 0,
        "_offset": 1,
        "_fallback_id": f"gc2:{correlation_id}",
    }
    report = writer.handle(writer_payload)

    assert report["rows_upserted"] == 1
    assert report["dod_verdict_rows"][0]["ticket_id"] == "OMN-20025"

    async def read_back() -> tuple[int, asyncpg.Record | None]:
        connection = await asyncpg.connect(binding.admin_dsn)
        try:
            count = await connection.fetchval(
                """
                SELECT count(*)
                FROM omninode_internal.dod_verify_runs
                WHERE goal_id = $1 AND contract_revision = $2
                """,
                goal_id,
                contract_revision,
            )
            row = await connection.fetchrow(
                """
                SELECT ticket_id, correlation_id, goal_id, parent_goal_id,
                       level, contract_revision
                FROM omninode_internal.dod_verify_runs
                WHERE goal_id = $1 AND contract_revision = $2
                """,
                goal_id,
                contract_revision,
            )
            return int(count), row
        finally:
            await connection.close()

    row_count, stored = asyncio.run(read_back())
    assert row_count == 1
    assert stored is not None
    assert stored["ticket_id"] == "OMN-20025"
    assert stored["correlation_id"] == correlation_id
    assert stored["goal_id"] == goal_id
    assert stored["parent_goal_id"] == parent_goal_id
    assert stored["level"] == "workflow_lane"
    assert stored["contract_revision"] == contract_revision
