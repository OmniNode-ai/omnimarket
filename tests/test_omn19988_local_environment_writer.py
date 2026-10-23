# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19988: the bus writer of local-environment reports, and the node contract that exposes them.

The Postgres writer applies the same pure plan the local store applies
(``plan_local_environment_write``), so these tests drive it through a stand-in
pool that keeps the table in memory and records every statement; the real
Postgres path is proven in hosted CI.

Failure modes each test is written against:

* F1 a report whose tenant is a slug, not a uuid, is stored: the dashboard reads
  by the install's tenant uuid, so the row would never be served (the run-trace
  writer's failure on OMN-19972). Producers resolve the tenant; the writer
  refuses anything else, so the runtime dead-letters it;
* F2 a replay writes a row or loses a committed delta; a late report publishes;
* F3 the siblings a report re-judges (a silent service turned STALE) are not
  written or not published, so the page keeps a stale verdict;
* F4 the transaction's tenant setting and the row's tenant disagree, so row
  security refuses the write;
* F5 two reports for one tenant's mode interleave between the read and the write;
* F6 the exposure's topic, key or columns differ from what the Overview panel
  and the API Keys card read;
* F8 the dashboard token reaches a served column.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import yaml

from omnimarket.nodes.node_projection_local_environment.handlers.handler_local_environment_writer import (
    LocalEnvironmentProjectionWriter,
)
from omnimarket.projection.discovery import load_projection_exposures_from_contract
from omnimarket.projection.runner import MessageMeta

pytestmark = pytest.mark.unit

NODE_DIR = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_local_environment"
)
CONTRACT_PATH = NODE_DIR / "contract.yaml"
STATUS_TOPIC = "onex.evt.omnimarket.local-environment-status.v1"
EXPOSURE_TOPIC = "onex.snapshot.projection.local-environment.v1"
TENANT = "6c24401e-0000-4000-8000-00000000000a"
T0 = datetime(2026, 10, 18, 6, 0, tzinfo=UTC)

# What omnidash dev reads from each row: src/pages/local/environment.tsx (the
# Overview Environment panel) and src/pages/local/access.tsx (the API Keys card).
PANEL_FIELDS = {
    "mode",
    "service",
    "verdict",
    "verdict_reason",
    "alert_code",
    "alert_severity",
    "alert_since",
    "last_seen",
}
CARD_FIELDS = {"mode", "service", "access_mode", "token_minted_at", "rotate_hint"}
HEADER_FIELDS = {"versions", "store_kind", "store_location", "last_change"}


def _report(
    service: str,
    state: str,
    at: datetime,
    *,
    tenant: str = TENANT,
    mode: str = "compose",
) -> dict[str, Any]:
    return {
        "tenant_id": tenant,
        "mode": mode,
        "service": service,
        "observed_state": state,
        "reason": f"{service} is {state}",
        "observed_at": at.isoformat(),
        "versions": {"omnimarket": "0.4.317"},
    }


class _Conn:
    """An in-memory local_environment table behind the statements the writer sends."""

    def __init__(
        self,
        table: dict[tuple[str, str, str], dict[str, Any]],
        registry: dict[str, str] | None = None,
    ) -> None:
        self.table = table
        # tenant_registry_mirror as {slug: uuid}; the tenant these tests write
        # under is recorded there, as the bundle's seed step records it.
        self.registry = registry if registry is not None else {"local-test": TENANT}
        self.statements: list[tuple[str, tuple[Any, ...]]] = []
        self.tenant_setting: str | None = None
        self.in_transaction = False
        self._cursor = max((r["projection_cursor"] for r in table.values()), default=0)

    @asynccontextmanager
    async def transaction(self) -> Any:
        self.in_transaction = True
        try:
            yield
        finally:
            self.in_transaction = False

    async def execute(self, sql: str, *args: Any) -> str:
        self.statements.append((sql, args))
        if "set_config" in sql:
            self.tenant_setting = str(args[1])
        return "OK"

    async def fetchval(self, sql: str, *args: Any) -> Any:
        """The registry mirror reads; not a recorded statement of the write."""
        assert "tenant_registry_mirror" in sql, sql
        if "WHERE tenant_uuid" in sql:
            found = [u for u in self.registry.values() if u == str(args[0])]
            return UUID(found[0]) if found else None
        if "WHERE tenant_slug" in sql:
            found_uuid = self.registry.get(str(args[0]))
            return UUID(found_uuid) if found_uuid else None
        raise AssertionError(f"unexpected registry read: {sql}")

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.statements.append((sql, args))
        if "FOR UPDATE" in sql:
            return [
                dict(row)
                for key, row in sorted(self.table.items())
                if key[0] == args[0] and key[1] == args[1]
            ]
        if sql.lstrip().upper().startswith("INSERT INTO PUBLIC.LOCAL_ENVIRONMENT"):
            columns = sql.split("(", 1)[1].split(")", 1)[0]
            names = [name.strip() for name in columns.split(",")]
            row = dict(zip(names, args, strict=True))
            # F4: row security would refuse a row whose tenant is not the
            # transaction's setting.
            assert row["tenant_id"] == self.tenant_setting, (row, self.tenant_setting)
            row["versions"] = json.loads(row["versions"])
            self._cursor += 1
            row["projection_cursor"] = self._cursor
            self.table[(row["tenant_id"], row["mode"], row["service"])] = row
            return [dict(row)]
        raise AssertionError(f"unexpected statement: {sql}")


class _Pool:
    def __init__(self, conn: _Conn) -> None:
        self._conn = conn

    @asynccontextmanager
    async def acquire(self) -> Any:
        yield self._conn


class _Db:
    def __init__(self, conn: _Conn) -> None:
        self.conn = conn
        self.pool = _Pool(conn)


def _writer(
    table: dict[tuple[str, str, str], dict[str, Any]],
    registry: dict[str, str] | None = None,
) -> tuple[LocalEnvironmentProjectionWriter, _Conn, list[dict[str, Any]]]:
    writer = LocalEnvironmentProjectionWriter()
    conn = _Conn(table, registry)
    patch = pytest.MonkeyPatch()
    patch.setattr(writer, "_db", _Db(conn))
    published: list[dict[str, Any]] = []

    async def _capture(exposure: Any, **kwargs: Any) -> bool:
        published.append({"topic": exposure.topic, **kwargs})
        return True

    patch.setattr(writer, "publish_snapshot_delta", _capture)
    return writer, conn, published


def _snapshot(table: dict[tuple[str, str, str], dict[str, Any]]) -> str:
    """The whole table, rows and times, as one comparable string."""
    return json.dumps(
        {"|".join(key): row for key, row in table.items()}, default=str, sort_keys=True
    )


def _project(writer: LocalEnvironmentProjectionWriter, report: dict[str, Any]) -> bool:
    meta = MessageMeta(partition=0, offset=7, fallback_id="t", topic=STATUS_TOPIC)
    return asyncio.run(writer.project_event(STATUS_TOPIC, dict(report), meta))


# ---- F1: the tenant ------------------------------------------------------------


@pytest.mark.parametrize(
    "tenant",
    [
        "local-ee67b16e7aab",  # a slug, as ONEX_TENANT_ID carries it
        "6C24401E-0000-4000-8000-00000000000A",  # a uuid, but not as stored
        "6c24401e000040008000000000000000a",  # not a uuid at all
    ],
)
def test_a_report_whose_tenant_is_not_a_canonical_uuid_is_refused_and_nothing_is_written(
    tenant: str,
) -> None:
    table: dict[tuple[str, str, str], dict[str, Any]] = {}
    writer, conn, published = _writer(table)
    with pytest.raises(ValueError, match="tenant"):
        _project(writer, _report("redpanda", "running", T0, tenant=tenant))
    assert table == {}
    assert published == []
    assert conn.statements == []


def test_a_report_with_a_canonical_uuid_tenant_is_stored_under_it() -> None:
    table: dict[tuple[str, str, str], dict[str, Any]] = {}
    writer, conn, published = _writer(table)
    assert _project(writer, _report("redpanda", "running", T0))
    assert list(table) == [(TENANT, "compose", "redpanda")]
    assert conn.tenant_setting == TENANT
    assert [p["tenant_id"] for p in published] == [TENANT]


# ---- F2: replays and late reports ---------------------------------------------


def test_a_replayed_report_writes_nothing_and_republishes_the_stored_row() -> None:
    table: dict[tuple[str, str, str], dict[str, Any]] = {}
    writer, conn, published = _writer(table)
    _project(writer, _report("redpanda", "running", T0))
    stored = _snapshot(table)
    original_delta = published[0]
    published.clear()

    replay = _report("redpanda", "running", T0)
    replay["reason"] = "redelivery must not replace stored content"
    _project(writer, replay)

    assert _snapshot(table) == stored
    assert published == [original_delta]
    inserts = [s for s, _ in conn.statements if s.lstrip().upper().startswith("INSERT")]
    assert len(inserts) == 1


def test_redelivery_repairs_a_publish_failure_after_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    table: dict[tuple[str, str, str], dict[str, Any]] = {}
    writer, conn, published = _writer(table)
    capture = writer.publish_snapshot_delta
    attempts: list[dict[str, Any]] = []

    async def fail_publish(exposure: Any, **kwargs: Any) -> bool:
        assert table
        assert not conn.in_transaction
        attempts.append(kwargs)
        raise RuntimeError("publish unavailable")

    monkeypatch.setattr(writer, "publish_snapshot_delta", fail_publish)
    report = _report("redpanda", "running", T0)
    with pytest.raises(RuntimeError, match="publish unavailable"):
        _project(writer, report)
    stored = _snapshot(table)
    assert len(attempts) == 1
    assert published == []
    conn.statements.clear()
    monkeypatch.setattr(writer, "publish_snapshot_delta", capture)

    _project(writer, report)

    assert _snapshot(table) == stored
    assert published == [{"topic": EXPOSURE_TOPIC, **attempts[0]}]
    assert not any(
        sql.lstrip().upper().startswith("INSERT") for sql, _ in conn.statements
    )


def test_a_report_older_than_the_stored_one_writes_nothing() -> None:
    table: dict[tuple[str, str, str], dict[str, Any]] = {}
    writer, _conn, published = _writer(table)
    _project(writer, _report("redpanda", "down", T0 + timedelta(seconds=30)))
    stored = _snapshot(table)
    published.clear()

    _project(writer, _report("redpanda", "running", T0))

    assert _snapshot(table) == stored
    assert published == []


# ---- F3: siblings the report re-judges -----------------------------------------


def test_a_report_after_the_deadline_turns_a_silent_sibling_stale_and_publishes_both() -> (
    None
):
    table: dict[tuple[str, str, str], dict[str, Any]] = {}
    writer, _conn, published = _writer(table)
    _project(writer, _report("postgres", "running", T0))
    _project(writer, _report("redpanda", "running", T0))
    published.clear()

    _project(writer, _report("redpanda", "running", T0 + timedelta(seconds=75)))

    assert table[(TENANT, "compose", "postgres")]["verdict"] == "stale"
    assert table[(TENANT, "compose", "postgres")]["alert_code"] == "HEALTH_UNKNOWN"
    assert table[(TENANT, "compose", "redpanda")]["verdict"] == "running"
    assert sorted(p["row"]["service"] for p in published) == ["postgres", "redpanda"]
    assert {p["topic"] for p in published} == {EXPOSURE_TOPIC}
    stored = _snapshot(table)
    original_deltas = {p["row"]["service"]: p for p in published}
    published.clear()
    _project(writer, _report("redpanda", "running", T0 + timedelta(seconds=75)))
    assert _snapshot(table) == stored
    assert {p["row"]["service"]: p for p in published} == original_deltas


def test_a_stopped_service_opens_one_service_down_alert_in_the_stored_row() -> None:
    table: dict[tuple[str, str, str], dict[str, Any]] = {}
    writer, _conn, published = _writer(table)
    _project(writer, _report("redpanda", "running", T0))
    _project(writer, _report("redpanda", "down", T0 + timedelta(seconds=15)))

    row = table[(TENANT, "compose", "redpanda")]
    assert (row["verdict"], row["alert_code"], row["alert_severity"]) == (
        "down",
        "SERVICE_DOWN",
        "critical",
    )
    assert published[-1]["row"]["alert_code"] == "SERVICE_DOWN"


# ---- F4 and F5: the transaction --------------------------------------------------


def test_the_tenant_setting_lock_and_read_come_before_the_write() -> None:
    table: dict[tuple[str, str, str], dict[str, Any]] = {}
    writer, conn, _published = _writer(table)
    _project(writer, _report("redpanda", "running", T0))
    kinds = []
    for sql, _args in conn.statements:
        if "set_config" in sql:
            kinds.append("tenant")
        elif "pg_advisory_xact_lock" in sql:
            kinds.append("lock")
        elif "FOR UPDATE" in sql:
            kinds.append("read")
        elif sql.lstrip().upper().startswith("INSERT"):
            kinds.append("write")
    assert kinds == ["tenant", "lock", "read", "write"]
    lock_args = next(a for s, a in conn.statements if "pg_advisory_xact_lock" in s)
    assert lock_args == (f"local_environment:{TENANT}:compose",)


# ---- the contract ----------------------------------------------------------------


def _contract() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    return loaded


def _exposure() -> Any:
    contract = _contract()
    exposures = load_projection_exposures_from_contract(
        contract, str(contract["name"]), CONTRACT_PATH
    )
    assert len(exposures) == 1
    return exposures[0]


def test_the_exposure_is_the_topic_the_panel_and_the_card_read() -> None:
    exposure = _exposure()
    assert exposure.topic == EXPOSURE_TOPIC
    assert exposure.table == "local_environment"
    assert tuple(exposure.key_columns) == ("tenant_id", "mode", "service")
    assert exposure.tenant_scoped


def test_the_exposure_serves_every_field_the_panel_the_header_and_the_card_read() -> (
    None
):
    columns = set(_exposure().columns)
    assert columns >= PANEL_FIELDS | CARD_FIELDS | HEADER_FIELDS


def test_no_served_column_could_carry_the_dashboard_token() -> None:
    columns = set(_exposure().columns)
    assert not {c for c in columns if "token" in c and c != "token_minted_at"}


def test_every_served_column_is_a_column_of_the_table() -> None:
    sql = (NODE_DIR / "migrations" / "0000_create_local_environment.sql").read_text()
    for column in _exposure().columns:
        assert f"ADD COLUMN IF NOT EXISTS {column} " in sql, column


def test_the_writer_subscribes_to_the_status_event_and_dead_letters_refusals() -> None:
    contract = _contract()
    assert contract["event_bus"]["subscribe_topics"] == [STATUS_TOPIC]
    assert contract["event_bus"]["dlq_topics"]
    assert LocalEnvironmentProjectionWriter().subscribe_topics == [STATUS_TOPIC]


def test_runtime_dispatch_resolves_only_the_in_process_writer() -> None:
    handlers = _contract()["handler_routing"]["handlers"]
    assert [h["handler"]["name"] for h in handlers] == [
        "LocalEnvironmentProjectionWriter"
    ]
    assert LocalEnvironmentProjectionWriter.onex_runtime_inprocess_dispatch is True
    assert _contract()["runtime_profiles"] == ["tenant-projection"]
