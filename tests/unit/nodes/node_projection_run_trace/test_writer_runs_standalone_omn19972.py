# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19972 amendment 5: the run-trace writer runs standalone, like the savings writer.

Jonah's ruling (OMN-19972, 2026-10-18) makes the writer its own process under
runtime profile ``projection-writer-run-trace``, reading the tenant mirror
through its own binding, because the tenant writer role holds no privilege on
the mirror and the runtime's in-process wiring allows one pool, one login
role.

Failure modes each test is written against:

* (J1) the writer is still claimed by the shared tenant-projection runtime as
  well as its own process, so two consumers fold the same steps;
* (J2) the mirror is read through the tenant writer's pool, which the deployed
  role cannot read once the hand grant is gone;
* (J4) the profile the contract names is not registered in the installed
  omnibase_core, so the runtime refuses the contract.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.constants.constants_runtime_profiles import (
    CONSUMER_ATTACHED_RUNTIME_PROFILES,
    REGISTERED_RUNTIME_PROFILES,
)
from omnibase_infra.runtime.auto_wiring.profile_ownership import (
    runtime_profile_owns_contract,
)

from omnimarket.nodes.node_projection_run_trace.handlers.handler_run_trace_writer import (
    RunTraceProjectionWriter,
)
from omnimarket.projection.runner import MessageMeta
from omnimarket.projection.tenant_registry_resolution import (
    TenantRegistryResolutionError,
)

_CHAIN = json.loads(
    (
        Path(__file__).resolve().parents[3] / "fixtures/omn19987/run_step_chain.json"
    ).read_text(encoding="utf-8")
)
_SLUG = "local-ee67b16e7aab"
_UUID = "97b9aa50-f53f-adb8-5404-5c7318dd4cb1"
_COLUMNS = (
    "tenant_id",
    "correlation_id",
    "step_sequence",
    "step_kind",
    "started_at",
    "duration_ms",
    "outcome",
    "failure_cause",
    "backend_id",
    "model_id",
    "attempt_number",
    "reason",
    "candidates",
    "failing_step_sequence",
)
_META = MessageMeta(
    partition=0,
    offset=11,
    fallback_id="run-step-0-11",
    topic="onex.evt.omnimarket.delegation-run-step.v1",
)


def _step(tenant: str) -> dict[str, Any]:
    return {**_CHAIN[1], "tenant_id": tenant}


class _Transaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_exc: object) -> bool:
        return False


class _Acquire:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def __aenter__(self) -> Any:
        return self._conn

    async def __aexit__(self, *_exc: object) -> bool:
        return False


class _Pool:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def acquire(self) -> _Acquire:
        return _Acquire(self._conn)


class _WriterConnection:
    """The writer's own pool: rows by key, but no privilege on the mirror."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str, int], dict[str, Any]] = {}
        self.session_tenants: list[str] = []

    def transaction(self) -> _Transaction:
        return _Transaction()

    async def execute(self, sql: str, *args: Any) -> None:
        if "set_config" in sql:
            self.session_tenants.append(str(args[1]))

    async def fetchval(self, sql: str, *args: Any) -> Any:
        if "tenant_registry_mirror" in sql:
            raise PermissionError("permission denied for table tenant_registry_mirror")
        return None

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        if "tenant_registry_mirror" in sql:
            raise PermissionError("permission denied for table tenant_registry_mirror")
        key = (str(args[0]), str(args[1]), int(args[2]))
        if "INSERT INTO public.run_trace" in sql:
            if key in self.rows:
                return []
            row = dict(zip(_COLUMNS, args, strict=True))
            row["updated_at"] = datetime(2026, 10, 17, tzinfo=UTC)
            row["projection_cursor"] = len(self.rows) + 1
            self.rows[key] = row
            return [dict(row)]
        assert sql.lstrip().upper().startswith("SELECT"), sql
        return [dict(self.rows[key])] if key in self.rows else []


class _MirrorDb:
    """The mirror's own binding: answers slug/uuid lookups, records every query."""

    def __init__(self, mirror: dict[str, str]) -> None:
        self._mirror = mirror
        self.queries: list[str] = []

    async def fetchval(self, sql: str, *args: Any) -> Any:
        self.queries.append(sql)
        if "tenant_slug = $1" in sql:
            return self._mirror.get(str(args[0]))
        if "tenant_uuid = $1" in sql:
            wanted = str(args[0])
            return wanted if wanted in self._mirror.values() else None
        return None


def _standalone_writer(
    writer_conn: _WriterConnection,
    mirror: dict[str, str],
    published: list[dict[str, Any]],
    calls: list[tuple[str, str]],
) -> tuple[RunTraceProjectionWriter, _MirrorDb]:
    mirror_db = _MirrorDb(mirror)
    writer_pool = _Pool(writer_conn)

    class _Writer(RunTraceProjectionWriter):
        def db_for(self, table: str, *, operation: str = "write") -> Any:
            calls.append((table, operation))
            if table == "tenant_registry_mirror":
                return mirror_db
            if table == "run_trace":
                return type("_WriterPool", (), {"pool": writer_pool})()
            raise AssertionError(f"unexpected table {table!r}")

        async def publish_snapshot_delta(self, exposure: Any, **kwargs: Any) -> bool:
            published.append(kwargs)
            return True

    return _Writer(), mirror_db


def test_only_its_own_runtime_profile_owns_the_contract() -> None:
    """(J1) its own profile owns the contract; the shared tenant-projection, main and effects runtimes do not, so no second consumer folds the same steps."""
    contract = yaml.safe_load(
        (
            Path(__file__).resolve().parents[4]
            / "src/omnimarket/nodes/node_projection_run_trace/contract.yaml"
        ).read_text(encoding="utf-8")
    )
    # no lane or node selection from the test process environment
    environ: dict[str, str] = {}
    assert (
        runtime_profile_owns_contract(
            contract, "projection-writer-run-trace", environ=environ
        )
        is True
    )
    for other in ("tenant-projection", "main", "effects"):
        assert (
            runtime_profile_owns_contract(contract, other, environ=environ) is False
        ), f"{other} must not own the run-trace writer"


def test_core_registers_the_profile_as_consumer_attached() -> None:
    """(J4) the profile the contract names is registered in the installed core."""
    assert "projection-writer-run-trace" in CONSUMER_ATTACHED_RUNTIME_PROFILES
    assert "projection-writer-run-trace" in REGISTERED_RUNTIME_PROFILES


async def test_the_mirror_is_read_through_its_own_binding_not_the_writer_pool() -> None:
    """(J2) the mirror is read through its own binding, not the writer pool."""
    writer_conn = _WriterConnection()
    published: list[dict[str, Any]] = []
    calls: list[tuple[str, str]] = []
    writer, mirror_db = _standalone_writer(
        writer_conn, {_SLUG: _UUID}, published, calls
    )

    row = await writer._project_step(_step(_SLUG), _META)

    assert row is not None
    assert row["tenant_id"] == _UUID
    assert [key[0] for key in writer_conn.rows] == [_UUID]
    assert writer_conn.session_tenants == [_UUID]
    assert ("tenant_registry_mirror", "read") in calls
    assert ("run_trace", "write") in calls
    assert mirror_db.queries
    assert [p["tenant_id"] for p in published] == [_UUID]


async def test_an_unknown_slug_is_refused_before_the_writer_pool_is_touched() -> None:
    """(J2) an unknown slug is refused before the writer pool is touched."""
    writer_conn = _WriterConnection()
    published: list[dict[str, Any]] = []
    calls: list[tuple[str, str]] = []
    writer, _ = _standalone_writer(writer_conn, {}, published, calls)

    with pytest.raises(TenantRegistryResolutionError):
        await writer._project_step(_step("local-unknown"), _META)

    assert writer_conn.rows == {}
    assert published == []
    assert ("run_trace", "write") not in calls
