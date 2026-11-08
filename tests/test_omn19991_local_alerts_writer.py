# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19991: local alert transitions published by the environment writer, folded by
node_projection_local_alerts into one row per (tenant, code, subject).

The environment writer derives each alert its write raised or cleared and
publishes it on onex.evt.omnimarket.local-alert-transitioned.v1. The local
alerts writer folds each transition through the same pure plan the local store
applies (``plan_local_alert_fold``). Both are driven here through stand-in
pools that keep the tables in memory and record every statement; the
migrations themselves are proven against real Postgres in
tests/test_omn19991_local_alerts_migrations.py.

Failure modes each test is written against:

* F1 a transition whose tenant is not a canonical uuid is stored: the
  dashboard reads by the install's tenant uuid, so the row is never served;
* F2 replaying the hour's transitions, or folding them in another order,
  changes a row; a repeated condition opens a second row;
* F3 an older transition arriving late overwrites a newer row, or publishes;
* F4 the write happens before the tenant setting, the key lock or the read;
* F5 the environment writer stores a DOWN but publishes no raise, or a
  recovery but no clear, or publishes a transition whose id differs on
  redelivery, so the fold cannot recognise it;
* F6 the exposure's topic, key or columns are not the table's.
"""

from __future__ import annotations

import asyncio
import json
import random
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.models.model_local_alert_transitioned import (
    ModelLocalAlertTransitioned,
)
from omnimarket.nodes.node_projection_local_alerts.handlers.handler_local_alerts_writer import (
    HandlerLocalAlertsProjectionRunner,
    LocalAlertsProjectionWriter,
    require_canonical_tenant,
)
from omnimarket.nodes.node_projection_local_environment.handlers.handler_local_environment_writer import (
    HandlerLocalEnvironmentProjectionRunner,
)
from omnimarket.projection.discovery import load_projection_exposures_from_contract
from omnimarket.projection.envelope import unwrap_envelope
from omnimarket.projection.error_classification import (
    ProjectionErrorClass,
    classify_projection_error,
)
from omnimarket.projection.runner import MessageMeta
from tests.test_omn19988_local_environment_writer import _Conn as _EnvConn
from tests.test_omn19988_local_environment_writer import _Db as _EnvDb

pytestmark = pytest.mark.unit

NODE_DIR = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_local_alerts"
)
CONTRACT_PATH = NODE_DIR / "contract.yaml"
TRANSITION_TOPIC = "onex.evt.omnimarket.local-alert-transitioned.v1"
STATUS_TOPIC = "onex.evt.omnimarket.local-environment-status.v1"
EXPOSURE_TOPIC = "onex.snapshot.projection.local-alerts.v1"
TENANT = "6c24401e-0000-4000-8000-00000000000a"
T0 = datetime(2026, 11, 6, 9, 0, tzinfo=UTC)

_Key = tuple[str, str, str]


class _AlertsConn:
    """An in-memory local_alerts table behind the statements the writer sends."""

    def __init__(self, table: dict[_Key, dict[str, Any]]) -> None:
        self.table = table
        self.statements: list[tuple[str, tuple[Any, ...]]] = []
        self.tenant_setting: str | None = None
        self.in_transaction = False
        self.published_in_transaction: list[bool] = []
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

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.statements.append((sql, args))
        if "FOR UPDATE" in sql:
            row = self.table.get((args[0], args[1], args[2]))
            return [dict(row)] if row is not None else []
        if sql.lstrip().upper().startswith("INSERT INTO PUBLIC.LOCAL_ALERTS"):
            columns = sql.split("(", 1)[1].split(")", 1)[0]
            names = [name.strip() for name in columns.split(",")]
            row = dict(zip(names, args, strict=True))
            # Row security would refuse a row whose tenant is not the setting.
            assert row["tenant_id"] == self.tenant_setting, (row, self.tenant_setting)
            # The table's own checks (0000_create_local_alerts.sql).
            assert (row["state"] == "cleared") == (row["cleared_at"] is not None)
            assert row["cleared_at"] is None or row["cleared_at"] >= row["raised_at"]
            for name in ("raised_at", "last_transition_at"):
                assert isinstance(row[name], datetime), name
            row["evidence"] = json.loads(row["evidence"])
            self._cursor += 1
            row["projection_cursor"] = self._cursor
            row["updated_at"] = T0
            self.table[(row["tenant_id"], row["code"], row["subject"])] = row
            return [dict(row)]
        raise AssertionError(f"unexpected statement: {sql}")


class _Pool:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    @asynccontextmanager
    async def acquire(self) -> Any:
        yield self._conn


class _AlertsDb:
    def __init__(self, conn: _AlertsConn) -> None:
        self.pool = _Pool(conn)


def _alerts_writer(
    table: dict[_Key, dict[str, Any]],
) -> tuple[HandlerLocalAlertsProjectionRunner, _AlertsConn, list[dict[str, Any]]]:
    writer = HandlerLocalAlertsProjectionRunner()
    conn = _AlertsConn(table)
    patch = pytest.MonkeyPatch()
    patch.setattr(writer, "_db", _AlertsDb(conn))
    published: list[dict[str, Any]] = []

    async def _capture(exposure: Any, **kwargs: Any) -> bool:
        conn.published_in_transaction.append(conn.in_transaction)
        published.append({"topic": exposure.topic, **kwargs})
        return True

    patch.setattr(writer, "publish_snapshot_delta", _capture)
    return writer, conn, published


def _fold(writer: HandlerLocalAlertsProjectionRunner, payload: dict[str, Any]) -> bool:
    meta = MessageMeta(partition=0, offset=3, fallback_id="t", topic=TRANSITION_TOPIC)
    return asyncio.run(writer.project_event(TRANSITION_TOPIC, dict(payload), meta))


def _snapshot(table: dict[_Key, dict[str, Any]]) -> str:
    rows = {
        "|".join(key): {k: v for k, v in row.items() if k != "projection_cursor"}
        for key, row in sorted(table.items())
    }
    return json.dumps(rows, default=str, sort_keys=True)


# ---- the environment writer publishes transitions --------------------------------


def _env_report(service: str, state: str, at: datetime) -> dict[str, Any]:
    return {
        "tenant_id": TENANT,
        "mode": "compose",
        "service": service,
        "observed_state": state,
        "reason": f"{service} is {state}",
        "observed_at": at.isoformat(),
        "versions": {"omnimarket": "0.4.317"},
    }


class _EnvironmentWriter:
    """The environment writer over an in-memory table, capturing what it publishes."""

    def __init__(self) -> None:
        self.table: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.runner = HandlerLocalEnvironmentProjectionRunner()
        self.conn = _EnvConn(self.table)
        self.bus: list[tuple[str, bytes]] = []
        self.fail_transition_publish = False
        patch = pytest.MonkeyPatch()
        patch.setattr(self.runner, "_db", _EnvDb(self.conn))

        async def _publish(topic: str, value: bytes) -> None:
            if self.fail_transition_publish:
                raise RuntimeError("bus unavailable")
            self.bus.append((topic, value))

        async def _snapshot_delta(exposure: Any, **kwargs: Any) -> bool:
            return True

        patch.setattr(self.runner, "_publish_fn", _publish)
        patch.setattr(self.runner, "publish_snapshot_delta", _snapshot_delta)

    def report(self, service: str, state: str, at: datetime) -> None:
        meta = MessageMeta(partition=0, offset=1, fallback_id="e", topic=STATUS_TOPIC)
        asyncio.run(
            self.runner.project_event(
                STATUS_TOPIC, _env_report(service, state, at), meta
            )
        )

    def payloads(self) -> list[dict[str, Any]]:
        """Each published transition as the projection runner receives it."""
        delivered = []
        for topic, value in self.bus:
            assert topic == TRANSITION_TOPIC
            data = unwrap_envelope(value)
            assert data is not None
            delivered.append(data)
        return delivered


def _directions(payloads: list[dict[str, Any]]) -> list[tuple[str, str, str]]:
    return [(p["code"], p["subject"], p["transition"]) for p in payloads]


def test_the_environment_writer_declares_the_transition_topic() -> None:
    assert HandlerLocalEnvironmentProjectionRunner().transition_topic == (
        TRANSITION_TOPIC
    )
    contract = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    assert contract["event_bus"]["subscribe_topics"] == [TRANSITION_TOPIC]


def test_a_stopped_service_publishes_a_raise_and_its_recovery_a_clear() -> None:
    env = _EnvironmentWriter()
    env.report("postgres", "running", T0)
    assert env.bus == []
    env.report("postgres", "down", T0 + timedelta(seconds=15))
    env.report("postgres", "running", T0 + timedelta(seconds=30))

    payloads = env.payloads()
    assert _directions(payloads) == [
        ("SERVICE_DOWN", "compose/postgres", "raised"),
        ("SERVICE_DOWN", "compose/postgres", "cleared"),
    ]
    raised, cleared = (
        ModelLocalAlertTransitioned.model_validate(_strip(p)) for p in payloads
    )
    assert raised.raised_at == T0 + timedelta(seconds=15)
    assert cleared.raised_at == raised.raised_at
    assert cleared.occurred_at == T0 + timedelta(seconds=30)
    assert payloads[0]["_envelope"]["tenant_id"] == TENANT
    assert payloads[0]["_envelope"]["correlation_id"] == str(raised.transition_id)


def test_a_repeated_down_report_publishes_nothing_more() -> None:
    env = _EnvironmentWriter()
    env.report("postgres", "down", T0)
    env.report("postgres", "down", T0 + timedelta(seconds=15))
    env.report("postgres", "down", T0 + timedelta(seconds=30))
    assert _directions(env.payloads()) == [
        ("SERVICE_DOWN", "compose/postgres", "raised")
    ]


def test_a_redelivered_report_re_raises_with_the_same_transition_id() -> None:
    env = _EnvironmentWriter()
    env.fail_transition_publish = True
    with pytest.raises(RuntimeError, match="bus unavailable"):
        env.report("postgres", "down", T0)
    # The transition is published inside the write's transaction: a failed
    # publish rolls the row back, so the redelivery plans the write again.
    assert (TENANT, "compose", "postgres") not in env.table
    env.fail_transition_publish = False

    env.report("postgres", "down", T0)
    env.report("postgres", "down", T0)

    (only,) = env.payloads()
    assert _directions([only]) == [("SERVICE_DOWN", "compose/postgres", "raised")]


def test_a_clear_whose_publish_failed_is_published_on_redelivery() -> None:
    """Tessl finding (OMN-23064): a clear lost when its publish failed after the commit."""
    env = _EnvironmentWriter()
    env.report("postgres", "down", T0)
    env.fail_transition_publish = True
    with pytest.raises(RuntimeError, match="bus unavailable"):
        env.report("postgres", "running", T0 + timedelta(seconds=30))
    env.fail_transition_publish = False
    env.report("postgres", "running", T0 + timedelta(seconds=30))
    assert _directions(env.payloads()) == [
        ("SERVICE_DOWN", "compose/postgres", "raised"),
        ("SERVICE_DOWN", "compose/postgres", "cleared"),
    ]


def test_a_replayed_report_publishes_no_transition_built_from_the_current_row() -> None:
    env = _EnvironmentWriter()
    env.report("postgres", "down", T0)
    assert len(env.bus) == 1
    env.report("postgres", "down", T0)
    assert len(env.bus) == 1


def test_no_publisher_for_a_pending_transition_fails_the_report_and_stores_nothing() -> (
    None
):
    """Tessl finding (OMN-23064): the report was acknowledged with its transition unpublished."""
    env = _EnvironmentWriter()

    async def _no_publisher() -> None:
        return None

    pytest.MonkeyPatch().setattr(env.runner, "get_publish_fn", _no_publisher)
    with pytest.raises(RuntimeError, match="publisher"):
        env.report("postgres", "down", T0)
    assert (TENANT, "compose", "postgres") not in env.table


# ---- F1: the tenant ------------------------------------------------------------


def _strip(payload: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in payload.items() if not k.startswith("_")}


def _transition_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "tenant_id": TENANT,
        "code": "SERVICE_DOWN",
        "subject": "compose/postgres",
        "transition": "raised",
        "severity": "critical",
        "raised_at": T0.isoformat(),
        "occurred_at": T0.isoformat(),
        "evidence": {
            "mode": "compose",
            "service": "postgres",
            "verdict": "down",
            "reason": "postgres is down",
            "last_seen": T0.isoformat(),
            "report_id": None,
        },
    }
    payload.update(overrides)
    return payload


@pytest.mark.parametrize(
    "tenant",
    [
        "local-ee67b16e7aab",
        "6C24401E-0000-4000-8000-00000000000A",
        "6c24401e000040008000000000000000a",
    ],
)
def test_a_transition_whose_tenant_is_not_a_canonical_uuid_is_refused_and_nothing_is_written(
    tenant: str,
) -> None:
    table: dict[_Key, dict[str, Any]] = {}
    writer, conn, published = _alerts_writer(table)
    with pytest.raises(ValueError, match="tenant"):
        _fold(writer, _transition_payload(tenant_id=tenant))
    assert table == {}
    assert published == []
    assert conn.statements == []


def test_a_transition_whose_id_does_not_match_its_fields_is_refused() -> None:
    table: dict[_Key, dict[str, Any]] = {}
    writer, conn, _published = _alerts_writer(table)
    with pytest.raises(ValidationError, match="transition_id"):
        _fold(
            writer,
            _transition_payload(transition_id="00000000-0000-4000-8000-000000000000"),
        )
    assert table == {}
    assert conn.statements == []


# ---- F2 and F3: one row per key, replays and late transitions --------------------


def _scripted_hour() -> list[dict[str, Any]]:
    """An hour of compose reports through the environment writer; its transitions."""
    env = _EnvironmentWriter()
    roster = ("omninode-runtime", "postgres", "redpanda", "valkey")
    for tick in range(120):
        at = T0 + timedelta(seconds=30 * tick)
        for service in roster:
            state = "running"
            if service == "postgres" and (10 <= tick < 14 or 70 <= tick < 75):
                state = "down"
            if service == "redpanda" and 40 <= tick < 46:
                state = "unhealthy"
            if service == "valkey" and 90 <= tick < 100:
                continue  # silent for five minutes
            env.report(service, state, at)
    return [_strip(p) for p in env.payloads()]


def test_replaying_every_transition_of_an_hour_changes_no_row() -> None:
    transitions = _scripted_hour()
    assert {(t["code"], t["subject"]) for t in transitions} == {
        ("SERVICE_DOWN", "compose/postgres"),
        ("SERVICE_DEGRADED", "compose/redpanda"),
        ("HEALTH_UNKNOWN", "compose/valkey"),
    }
    table: dict[_Key, dict[str, Any]] = {}
    writer, _conn, _published = _alerts_writer(table)
    for transition in transitions:
        _fold(writer, transition)
    # postgres went down twice: one row, updated in place, cleared at the end.
    assert sorted(table) == [
        (TENANT, "HEALTH_UNKNOWN", "compose/valkey"),
        (TENANT, "SERVICE_DEGRADED", "compose/redpanda"),
        (TENANT, "SERVICE_DOWN", "compose/postgres"),
    ]
    assert {row["state"] for row in table.values()} == {"cleared"}
    rows = _snapshot(table)

    for transition in transitions:
        _fold(writer, transition)
    assert _snapshot(table) == rows

    for seed in (1, 2):
        shuffled = list(transitions)
        random.Random(seed).shuffle(shuffled)
        other: dict[_Key, dict[str, Any]] = {}
        other_writer, _c, _p = _alerts_writer(other)
        for transition in shuffled + list(reversed(shuffled)):
            _fold(other_writer, transition)
        assert _snapshot(other) == rows


def test_a_clear_updates_the_raised_row_in_place() -> None:
    table: dict[_Key, dict[str, Any]] = {}
    writer, _conn, published = _alerts_writer(table)
    raised = _transition_payload()
    cleared = _transition_payload(
        transition="cleared", occurred_at=(T0 + timedelta(seconds=30)).isoformat()
    )
    _fold(writer, raised)
    _fold(writer, cleared)
    assert list(table) == [(TENANT, "SERVICE_DOWN", "compose/postgres")]
    row = table[(TENANT, "SERVICE_DOWN", "compose/postgres")]
    assert row["state"] == "cleared"
    assert row["cleared_at"] == T0 + timedelta(seconds=30)
    assert [p["row"]["state"] for p in published] == ["raised", "cleared"]
    assert {p["topic"] for p in published} == {EXPOSURE_TOPIC}


def test_an_older_transition_writes_and_publishes_nothing() -> None:
    table: dict[_Key, dict[str, Any]] = {}
    writer, conn, published = _alerts_writer(table)
    _fold(
        writer,
        _transition_payload(
            transition="cleared", occurred_at=(T0 + timedelta(seconds=30)).isoformat()
        ),
    )
    rows = _snapshot(table)
    published.clear()
    _fold(writer, _transition_payload())
    assert _snapshot(table) == rows
    assert published == []
    inserts = [s for s, _ in conn.statements if s.lstrip().upper().startswith("INSERT")]
    assert len(inserts) == 1


def test_a_replayed_transition_writes_nothing_and_republishes_the_stored_row() -> None:
    table: dict[_Key, dict[str, Any]] = {}
    writer, conn, published = _alerts_writer(table)
    _fold(writer, _transition_payload())
    original = published[0]
    published.clear()
    _fold(writer, _transition_payload())
    assert published == [original]
    inserts = [s for s, _ in conn.statements if s.lstrip().upper().startswith("INSERT")]
    assert len(inserts) == 1


# ---- F4: the transaction ---------------------------------------------------------


def test_the_tenant_setting_lock_and_read_come_before_the_write() -> None:
    table: dict[_Key, dict[str, Any]] = {}
    writer, conn, _published = _alerts_writer(table)
    _fold(writer, _transition_payload())
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
    assert lock_args == (f"local_alerts:{TENANT}:SERVICE_DOWN:compose/postgres",)
    upsert = next(s for s, _ in conn.statements if s.lstrip().startswith("INSERT"))
    assert "ON CONFLICT (tenant_id, code, subject) DO UPDATE" in upsert
    assert "nextval('public.local_alerts_projection_cursor_seq')" in upsert


# ---- F6: the contract ------------------------------------------------------------


def _contract() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    return loaded


def test_the_exposure_is_keyed_on_tenant_code_and_subject() -> None:
    contract = _contract()
    exposures = load_projection_exposures_from_contract(
        contract, str(contract["name"]), CONTRACT_PATH
    )
    assert len(exposures) == 1
    exposure = exposures[0]
    assert exposure.topic == EXPOSURE_TOPIC
    assert exposure.table == "local_alerts"
    assert tuple(exposure.key_columns) == ("tenant_id", "code", "subject")
    assert exposure.tenant_scoped
    sql = (NODE_DIR / "migrations" / "0000_create_local_alerts.sql").read_text()
    for column in exposure.columns:
        assert f"ADD COLUMN IF NOT EXISTS {column} " in sql, column


def test_runtime_dispatch_resolves_only_the_in_process_writer() -> None:
    contract = _contract()
    handlers = contract["handler_routing"]["handlers"]
    assert [h["handler"]["name"] for h in handlers] == ["LocalAlertsProjectionWriter"]
    assert LocalAlertsProjectionWriter.onex_runtime_inprocess_dispatch is True
    assert not getattr(
        HandlerLocalAlertsProjectionRunner, "onex_runtime_inprocess_dispatch", False
    )
    assert contract["runtime_profiles"] == ["tenant-projection"]
    assert contract["event_bus"]["dlq_topics"]


def test_a_refused_tenant_is_classified_poison_not_retried() -> None:
    """Tessl finding (OMN-23064): a bare ValueError classified RECOVERABLE and was retried."""
    with pytest.raises(ValueError, match="tenant") as refused:
        require_canonical_tenant("local-ee67b16e7aab")
    assert classify_projection_error(refused.value) is ProjectionErrorClass.POISON


def test_the_snapshot_delta_is_published_while_the_key_lock_is_held() -> None:
    """Tessl finding (OMN-23064): the delta left after the lock ended, so deltas for one key reordered."""
    table: dict[_Key, dict[str, Any]] = {}
    writer, conn, published = _alerts_writer(table)
    _fold(writer, _transition_payload())
    assert len(published) == 1
    assert conn.published_in_transaction == [True]
    published.clear()
    conn.published_in_transaction.clear()
    _fold(writer, _transition_payload())
    assert conn.published_in_transaction == [True]


def test_a_failed_snapshot_publish_is_published_by_the_redelivery() -> None:
    table: dict[_Key, dict[str, Any]] = {}
    writer, _conn, published = _alerts_writer(table)
    capture = writer.publish_snapshot_delta

    async def _fail(exposure: Any, **kwargs: Any) -> bool:
        raise RuntimeError("publish unavailable")

    pytest.MonkeyPatch().setattr(writer, "publish_snapshot_delta", _fail)
    with pytest.raises(RuntimeError, match="publish unavailable"):
        _fold(writer, _transition_payload())
    pytest.MonkeyPatch().setattr(writer, "publish_snapshot_delta", capture)
    _fold(writer, _transition_payload())
    assert len(published) == 1
