# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC3 E2E: retained synthetic records through the real handler into Postgres.

The local PostgreSQL 16 cluster is owned by this test and listens on a private
Unix socket. The existing migration harness builds both source and replay
schemas. No source rows are copied; broker fixture records drive the replay.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    HandlerProjectionDelegation,
)
from omnimarket.projection.ac3_replay import (
    CapturedRecord,
    OffsetSpan,
    ReplayError,
    compare_tenant_keys,
    replay_synthetic_records,
)
from omnimarket.projection.postgres_sync_database import PostgresSyncProjectionAdapter
from scripts import verify_omn15359_ac3_replay as command
from tests.test_omn19514_ticket_id_projection_real_postgres import (
    _NullPublisher,
    _Postgres,
    _provisioned,
)

pytestmark = pytest.mark.integration

_TOPIC = "onex.evt.omnibase-infra.delegation-completed.v1"
_FAILED_TOPIC = "onex.evt.omnibase-infra.delegation-failed.v1"
_TENANTS = {
    "omn15359-a": UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
    "omn15359-b": UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
}
_CORRELATIONS = {
    "a1": "15359000-0000-4000-8000-000000000001",
    "a2": "15359000-0000-4000-8000-000000000002",
    "b1": "15359000-0000-4000-8000-000000000003",
}
_EXPECTED = {
    str(_TENANTS["omn15359-a"]): {_CORRELATIONS["a1"], _CORRELATIONS["a2"]},
    str(_TENANTS["omn15359-b"]): {_CORRELATIONS["b1"]},
}


def _pg_bin(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    for prefix in sorted(Path("/opt/homebrew/opt").glob("postgresql@*"), reverse=True):
        candidate = prefix / "bin" / name
        if candidate.exists():
            return str(candidate)
    return None


@pytest.fixture(scope="module")
def local_postgres() -> Iterator[tuple[_Postgres, str]]:
    """Disposable native PostgreSQL 16; a missing server is an explicit skip."""
    initdb, pg_ctl = _pg_bin("initdb"), _pg_bin("pg_ctl")
    if not initdb or not pg_ctl:
        pytest.skip("native initdb/pg_ctl unavailable")
    root = Path(tempfile.mkdtemp(prefix="omn15359-pg-"))
    data, sock = root / "data", root / "sock"
    sock.mkdir()
    env = {"PATH": os.environ.get("PATH", ""), "LANG": "C", "LC_ALL": "C"}
    try:
        initialized = subprocess.run(
            [
                initdb,
                "-D",
                str(data),
                "-U",
                "postgres",
                "--auth-local=trust",
                "-E",
                "UTF8",
            ],
            capture_output=True,
            env=env,
        )
        if initialized.returncode:
            raise RuntimeError(
                f"initdb failed: {initialized.stderr.decode(errors='replace')}"
            )
        started = subprocess.run(
            [
                pg_ctl,
                "-D",
                str(data),
                "-l",
                str(root / "postgres.log"),
                "-o",
                f"-k {sock} -h ''",
                "-w",
                "start",
            ],
            capture_output=True,
            env=env,
        )
        if started.returncode:
            raise RuntimeError(
                f"pg_ctl failed: {started.stderr.decode(errors='replace')}"
            )
        yield (
            _Postgres(host=str(sock), port=5432, database="postgres"),
            (f"host={sock} dbname=postgres user=postgres"),
        )
    finally:
        if data.exists():
            subprocess.run(
                [pg_ctl, "-D", str(data), "-m", "immediate", "-w", "stop"],
                check=False,
                capture_output=True,
                env=env,
            )
        shutil.rmtree(root, ignore_errors=True)


async def _seed_mirror(admin: object, schema: str) -> None:
    await admin.execute(  # type: ignore[attr-defined]
        f"CREATE TABLE {schema}.tenant_registry_mirror ("
        "tenant_slug text PRIMARY KEY, tenant_uuid uuid UNIQUE NOT NULL)"
    )
    for slug, tenant_uuid in _TENANTS.items():
        await admin.execute(  # type: ignore[attr-defined]
            f"INSERT INTO {schema}.tenant_registry_mirror (tenant_slug, tenant_uuid) "
            "VALUES ($1, $2)",
            slug,
            tenant_uuid,
        )


async def _seed_full_mirror(admin: object, schema: str) -> None:
    await admin.execute(  # type: ignore[attr-defined]
        f"CREATE TABLE {schema}.tenant_registry_mirror ("
        "tenant_slug text PRIMARY KEY, tenant_uuid uuid UNIQUE NOT NULL, "
        "display_name text, status text NOT NULL, registry_created_at timestamptz, "
        "observed_at timestamptz NOT NULL DEFAULT now(), source_event_id text)"
    )
    for slug, tenant_uuid in _TENANTS.items():
        await admin.execute(  # type: ignore[attr-defined]
            f"INSERT INTO {schema}.tenant_registry_mirror "
            "(tenant_slug, tenant_uuid, display_name, status, source_event_id) "
            "VALUES ($1, $2, $1, 'active', $1)",
            slug,
            tenant_uuid,
        )


def _record(
    tenant: UUID, correlation: str, offset: int, *, topic: str = _TOPIC
) -> CapturedRecord:
    return CapturedRecord(
        topic=topic,
        partition=0,
        offset=offset,
        payload={
            "tenant_id": str(tenant),
            "payload": {
                "status": "failed" if topic == _FAILED_TOPIC else "completed",
                "correlation_id": correlation,
                "task_type": "research",
                "tenant_id": str(tenant),
                "metrics": {"cost_usd": 0.0},
            },
        },
    )


async def _keys(admin: object, schema: str) -> list[dict[str, str]]:
    rows = await admin.fetch(  # type: ignore[attr-defined]
        f"SELECT tenant_id::text AS tenant_id, correlation_id::text AS correlation_id FROM {schema}.delegation_events "
        "WHERE correlation_id = ANY($1::text[]) ORDER BY tenant_id, correlation_id",
        list(_CORRELATIONS.values()),
    )
    return [dict(row) for row in rows]


def _schema_dsn(postgres: _Postgres, schema: str) -> str:
    return (
        f"host={postgres.host} port={postgres.port} dbname={postgres.database} "
        f"user={postgres.user} options='-c search_path={schema}'"
    )


@pytest.mark.asyncio
async def test_replay_two_tenants_into_two_empty_copies_is_repeatable(
    local_postgres: tuple[_Postgres, str],
) -> None:
    postgres, dsn = local_postgres
    records = [
        _record(_TENANTS["omn15359-a"], _CORRELATIONS["a1"], 4),
        _record(
            _TENANTS["omn15359-b"],
            _CORRELATIONS["b1"],
            5,
            topic=_FAILED_TOPIC,
        ),
        _record(_TENANTS["omn15359-a"], _CORRELATIONS["a2"], 6),
        _record(_TENANTS["omn15359-a"], _CORRELATIONS["a1"], 7),
    ]
    handler = HandlerProjectionDelegation(publisher=_NullPublisher())
    async with _provisioned(postgres) as (source_admin, source_schema):
        await _seed_mirror(source_admin, source_schema)
        source_adapter = PostgresSyncProjectionAdapter(dsn, schema=source_schema)
        replay_synthetic_records(records, _EXPECTED, source_adapter, handler=handler)
        live = await _keys(source_admin, source_schema)
        assert len(live) == 3  # duplicate delivery remained idempotent
        digests: list[dict[str, str]] = []
        for _ in range(2):
            async with _provisioned(postgres) as (replay_admin, replay_schema):
                await _seed_mirror(replay_admin, replay_schema)
                assert await _keys(replay_admin, replay_schema) == []
                replay_adapter = PostgresSyncProjectionAdapter(
                    dsn, schema=replay_schema
                )
                replay_synthetic_records(
                    records, _EXPECTED, replay_adapter, handler=handler
                )
                replay = await _keys(replay_admin, replay_schema)
                comparison = compare_tenant_keys(
                    _EXPECTED, live_rows=live, replay_rows=replay
                )
                digests.append(
                    {
                        tenant: row.replay_key_sha256
                        for tenant, row in comparison.items()
                    }
                )
                with pytest.raises(ReplayError, match="not empty"):
                    replay_synthetic_records(
                        records, _EXPECTED, replay_adapter, handler=handler
                    )
        assert digests[0] == digests[1]

        async with _provisioned(postgres) as (refusal_admin, refusal_schema):
            await _seed_mirror(refusal_admin, refusal_schema)
            refusal_adapter = PostgresSyncProjectionAdapter(dsn, schema=refusal_schema)
            with pytest.raises(ReplayError, match="absent from retained window"):
                replay_synthetic_records(
                    records[:2], _EXPECTED, refusal_adapter, handler=handler
                )
            assert await _keys(refusal_admin, refusal_schema) == []
            wrong_tenant = _record(_TENANTS["omn15359-b"], _CORRELATIONS["a1"], 4)
            with pytest.raises(ReplayError, match="different tenant"):
                replay_synthetic_records(
                    [wrong_tenant, *records[1:]],
                    _EXPECTED,
                    refusal_adapter,
                    handler=handler,
                )
            assert await _keys(refusal_admin, refusal_schema) == []


@pytest.mark.asyncio
async def test_executable_writes_a_hashed_two_tenant_receipt_from_an_empty_copy(
    local_postgres: tuple[_Postgres, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    postgres, _ = local_postgres
    records = [
        _record(_TENANTS["omn15359-a"], _CORRELATIONS["a1"], 4),
        _record(
            _TENANTS["omn15359-b"],
            _CORRELATIONS["b1"],
            5,
            topic=_FAILED_TOPIC,
        ),
        _record(_TENANTS["omn15359-a"], _CORRELATIONS["a2"], 6),
    ]

    async def retained(
        *args: object, **kwargs: object
    ) -> tuple[list[CapturedRecord], list[OffsetSpan]]:
        return records, [
            OffsetSpan(_TOPIC, 0, 4, 7, 7),
            OffsetSpan(_FAILED_TOPIC, 0, 0, 6, 6),
        ]

    monkeypatch.setattr(command, "capture_retained", retained)
    monkeypatch.setenv("AC3_LANE_IDENTITY", "operator-201")
    monkeypatch.setenv("ONEX_ENVIRONMENT", "lakshman")
    monkeypatch.setenv("KAFKA_BOOTSTRAP_SERVERS", "unused.fixture:9092")
    async with _provisioned(postgres) as (source_admin, source_schema):
        await _seed_mirror(source_admin, source_schema)
        source_adapter = PostgresSyncProjectionAdapter(
            _schema_dsn(postgres, source_schema)
        )
        replay_synthetic_records(
            records,
            _EXPECTED,
            source_adapter,
            handler=HandlerProjectionDelegation(publisher=_NullPublisher()),
        )
        async with _provisioned(postgres) as (replay_admin, replay_schema):
            await _seed_mirror(replay_admin, replay_schema)
            monkeypatch.setenv("AC3_SOURCE_DSN", _schema_dsn(postgres, source_schema))
            monkeypatch.setenv("AC3_REPLAY_DSN", _schema_dsn(postgres, replay_schema))
            output = tmp_path / "ac3-local-replay-receipt.json"
            with pytest.raises(ReplayError, match="measured sha256"):
                await command._execute(
                    lane="operator-201",
                    expected=_EXPECTED,
                    writer_image="mutable-tag",
                    deployed_handler_sha256=command._digest(command.HANDLER),
                    receipt_path=output,
                    replay_schema=replay_schema,
                )
            assert await _keys(replay_admin, replay_schema) == []
            monkeypatch.setenv("AC3_REPLAY_DSN", _schema_dsn(postgres, source_schema))
            with pytest.raises(ReplayError, match="same relation"):
                await command._execute(
                    lane="operator-201",
                    expected=_EXPECTED,
                    writer_image="local-e2e-fixture@sha256:" + "a" * 64,
                    deployed_handler_sha256=command._digest(command.HANDLER),
                    receipt_path=output,
                    replay_schema=source_schema,
                )
            monkeypatch.setenv("AC3_REPLAY_DSN", _schema_dsn(postgres, replay_schema))
            receipt = await command._execute(
                lane="operator-201",
                expected=_EXPECTED,
                writer_image="local-e2e-fixture@sha256:" + "a" * 64,
                deployed_handler_sha256=command._digest(command.HANDLER),
                receipt_path=output,
                replay_schema=replay_schema,
            )
            assert receipt["result"] == "PASS"
            assert receipt["empty_target_rows_before"] == 0
            assert len(receipt["tenants"]) == 2
            assert len(receipt["offsets"]) == 2
            assert receipt["offsets"][0] == {
                "topic": _TOPIC,
                "partition": 0,
                "start": 4,
                "end": 7,
                "final": 7,
            }
            assert output.read_text(encoding="utf-8").endswith("\n")
            assert len(command._digest(output)) == 64
            assert len(await _keys(replay_admin, replay_schema)) == 3


@pytest.mark.asyncio
async def test_provision_target_clones_shape_and_registry_but_no_projection_rows(
    local_postgres: tuple[_Postgres, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    postgres, dsn = local_postgres
    target_schema = f"omn15359_ac3_{uuid4().hex[:12]}"
    async with _provisioned(postgres) as (source_admin, source_schema):
        await _seed_full_mirror(source_admin, source_schema)
        source_adapter = PostgresSyncProjectionAdapter(dsn, schema=source_schema)
        replay_synthetic_records(
            [
                _record(_TENANTS["omn15359-a"], _CORRELATIONS["a1"], 1),
                _record(_TENANTS["omn15359-a"], _CORRELATIONS["a2"], 2),
                _record(
                    _TENANTS["omn15359-b"],
                    _CORRELATIONS["b1"],
                    3,
                    topic=_FAILED_TOPIC,
                ),
            ],
            _EXPECTED,
            source_adapter,
            handler=HandlerProjectionDelegation(publisher=_NullPublisher()),
        )
        monkeypatch.setenv("AC3_SOURCE_DSN", _schema_dsn(postgres, source_schema))
        monkeypatch.setenv("AC3_REPLAY_ADMIN_DSN", dsn)
        monkeypatch.setenv("AC3_LANE_IDENTITY", "operator-201")
        monkeypatch.setenv("ONEX_ENVIRONMENT", "lakshman")
        receipt_path = tmp_path / "provision.json"
        receipt = command._provision_target(
            lane="operator-201",
            expected=_EXPECTED,
            replay_schema=target_schema,
            receipt_path=receipt_path,
        )
        try:
            assert receipt["empty_target_rows"] == 0
            assert receipt["registry_rows"] == 2
            assert (
                await source_admin.fetchval(
                    f"SELECT count(*) FROM {source_schema}.delegation_events"
                )
                == 3
            )
            assert (
                await source_admin.fetchval(
                    f"SELECT count(*) FROM {target_schema}.delegation_events"
                )
                == 0
            )
            assert (
                await source_admin.fetchval(
                    f"SELECT count(*) FROM {target_schema}.tenant_registry_mirror"
                )
                == 2
            )
        finally:
            await source_admin.execute(f"DROP SCHEMA IF EXISTS {target_schema} CASCADE")


@pytest.mark.asyncio
async def test_provision_target_refuses_missing_mapping_before_schema_creation(
    local_postgres: tuple[_Postgres, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    postgres, dsn = local_postgres
    target_schema = f"omn15359_ac3_{uuid4().hex[:12]}"
    async with _provisioned(postgres) as (source_admin, source_schema):
        await _seed_full_mirror(source_admin, source_schema)
        await source_admin.execute(
            f"DELETE FROM {source_schema}.tenant_registry_mirror WHERE tenant_uuid = $1",
            _TENANTS["omn15359-b"],
        )
        monkeypatch.setenv("AC3_SOURCE_DSN", _schema_dsn(postgres, source_schema))
        monkeypatch.setenv("AC3_REPLAY_ADMIN_DSN", dsn)
        monkeypatch.setenv("AC3_LANE_IDENTITY", "operator-201")
        monkeypatch.setenv("ONEX_ENVIRONMENT", "lakshman")
        with pytest.raises(ReplayError, match="registry mapping is missing"):
            command._provision_target(
                lane="operator-201",
                expected=_EXPECTED,
                replay_schema=target_schema,
                receipt_path=tmp_path / "missing.json",
            )
        assert (
            await source_admin.fetchval(
                "SELECT to_regnamespace($1)::oid", target_schema
            )
            is None
        )
