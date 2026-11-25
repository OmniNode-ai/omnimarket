# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26663: every lab alert is one bus event; node_projection_lab_alerts folds them into one row each.

A source raises or clears an alert by publishing one ``lab-alert-transitioned`` event. The projection
folds each event into one row per (source, subject), open or cleared, with severity, owner and times, and
publishes the written row to the bus-backed snapshot topic the dashboard reads. These tests drive the pure
fold and the writer through an in-memory stand-in for the table.

Failure modes each test is written against:

* F1 an alert with no owner or no severity is stored, so the dashboard shows an alert nobody owns;
* F2 replaying a source's transitions, or folding them in another order, changes a row or opens a second;
* F3 an older transition arriving late overwrites a newer row;
* F4 a clear does not carry the clear time, or a clear before the raise is accepted;
* F5 the exposure's topic, key or columns are not the table's, or the shape document leaves a column without a
  stated meaning;
* F6 the node is not registered, so the runtime never discovers it.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.enums.enum_lab_alert_transition import EnumLabAlertTransition
from omnimarket.models.model_lab_alert_transitioned import (
    ModelLabAlertTransitioned,
    lab_alert_id,
)
from omnimarket.nodes.node_projection_lab_alerts.handlers.handler_lab_alerts_writer import (
    HandlerLabAlertsProjectionRunner,
    LabAlertsProjectionWriter,
)
from omnimarket.nodes.node_projection_lab_alerts.handlers.handler_projection_lab_alerts import (
    HandlerProjectionLabAlerts,
)
from omnimarket.nodes.node_projection_lab_alerts.models.model_lab_alerts_plan import (
    ModelLabAlertsPlanRequest,
)
from omnimarket.projection.discovery import load_projection_exposures_from_contract
from omnimarket.projection.runner import MessageMeta

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
NODE_DIR = ROOT / "src" / "omnimarket" / "nodes" / "node_projection_lab_alerts"
CONTRACT_PATH = NODE_DIR / "contract.yaml"
SHAPE_DOC = ROOT / "docs" / "reference" / "lab-alerts-projection-read.yaml"
TRANSITION_TOPIC = "onex.evt.omnimarket.lab-alert-transitioned.v1"
EXPOSURE_TOPIC = "onex.snapshot.projection.lab.alerts.v1"
T0 = datetime(2026, 11, 24, 9, 0, tzinfo=UTC)


def _payload(
    *,
    transition: str = "raised",
    source: str = "dev_tag_heartbeat",
    subject: str = "lane-dev",
    severity: str = "SEV2",
    owner: str = "lab-floor",
    detail: str = "dev tag has not moved for 2 hours",
    raised_at: datetime = T0,
    occurred_at: datetime | None = None,
    **extra: Any,
) -> dict[str, Any]:
    at = occurred_at or raised_at
    return {
        "source": source,
        "subject": subject,
        "transition": transition,
        "severity": severity,
        "owner": owner,
        "detail": detail,
        "raised_at": raised_at.isoformat(),
        "occurred_at": at.isoformat(),
        **extra,
    }


def _model(**kwargs: Any) -> ModelLabAlertTransitioned:
    return ModelLabAlertTransitioned.model_validate(_payload(**kwargs))


def _clear(after: timedelta = timedelta(minutes=5), **kwargs: Any) -> dict[str, Any]:
    return _payload(
        transition="cleared", occurred_at=kwargs.pop("raised_at", T0) + after, **kwargs
    )


# ---- the event: F1, F4 ------------------------------------------------------------


def test_a_transition_id_is_derived_from_what_the_event_says() -> None:
    first = _model()
    again = _model()
    assert first.transition_id == again.transition_id
    assert first.transition_id != _model(subject="lane-other").transition_id
    assert first.alert_id == lab_alert_id("dev_tag_heartbeat", "lane-dev")


def test_a_transition_whose_id_does_not_match_its_fields_is_refused() -> None:
    with pytest.raises(ValidationError, match="transition_id"):
        _model(transition_id="00000000-0000-4000-8000-000000000000")


@pytest.mark.parametrize("field", ["owner", "severity", "source", "subject", "detail"])
def test_an_alert_missing_a_field_the_dashboard_shows_is_refused(field: str) -> None:
    payload = _payload()
    del payload[field]
    with pytest.raises(ValidationError, match=field):
        ModelLabAlertTransitioned.model_validate(payload)
    payload[field] = "   "
    with pytest.raises(ValidationError):
        ModelLabAlertTransitioned.model_validate(payload)


def test_a_severity_outside_the_lab_levels_is_refused() -> None:
    with pytest.raises(ValidationError, match="severity"):
        _model(severity="urgent")


def test_a_clear_before_the_raise_is_refused() -> None:
    with pytest.raises(ValidationError, match="before it was raised"):
        _model(transition="cleared", occurred_at=T0 - timedelta(seconds=1))


def test_a_raise_occurs_at_the_time_the_alert_is_raised() -> None:
    with pytest.raises(ValidationError, match="raise occurs at"):
        _model(occurred_at=T0 + timedelta(seconds=1))


def test_a_timestamp_without_a_zone_is_refused() -> None:
    with pytest.raises(ValidationError):
        _model(raised_at=datetime(2026, 11, 24, 9, 0), occurred_at=None)


# ---- the pure fold: F2, F3, F4 ----------------------------------------------------


def _plan(
    transition: dict[str, Any], stored: dict[str, object] | None
) -> dict[str, object] | None:
    return (
        HandlerProjectionLabAlerts()
        .handle(
            ModelLabAlertsPlanRequest(
                transition=ModelLabAlertTransitioned.model_validate(transition),
                stored_row=stored,
            )
        )
        .row
    )


def test_a_raise_plans_an_open_row_with_severity_owner_and_raise_time() -> None:
    row = _plan(_payload(), None)
    assert row is not None
    assert row["state"] == "open"
    assert row["severity"] == "SEV2"
    assert row["owner"] == "lab-floor"
    assert row["source"] == "dev_tag_heartbeat"
    assert row["subject"] == "lane-dev"
    assert row["raised_at"] == T0.isoformat()
    assert row["cleared_at"] is None
    assert row["alert_id"] == lab_alert_id("dev_tag_heartbeat", "lane-dev")


def test_a_clear_plans_the_same_row_cleared_with_its_clear_time() -> None:
    opened = _plan(_payload(), None)
    cleared = _plan(_clear(), opened)
    assert cleared is not None
    assert cleared["alert_id"] == opened["alert_id"] if opened else False
    assert cleared["state"] == "cleared"
    assert cleared["raised_at"] == T0.isoformat()
    assert cleared["cleared_at"] == (T0 + timedelta(minutes=5)).isoformat()


def test_a_replayed_transition_plans_nothing() -> None:
    opened = _plan(_payload(), None)
    assert _plan(_payload(), opened) is None


def test_an_older_transition_plans_nothing() -> None:
    cleared = _plan(_clear(), _plan(_payload(), None))
    assert _plan(_payload(), cleared) is None


def test_a_raise_after_a_clear_opens_a_new_episode_on_the_same_row() -> None:
    cleared = _plan(_clear(), _plan(_payload(), None))
    later = T0 + timedelta(hours=1)
    reopened = _plan(_payload(raised_at=later), cleared)
    assert reopened is not None
    assert reopened["state"] == "open"
    assert reopened["raised_at"] == later.isoformat()
    assert reopened["cleared_at"] is None
    assert reopened["alert_id"] == cleared["alert_id"] if cleared else False


# ---- the writer: F2, F3 -----------------------------------------------------------

_Key = str


class _AlertsConn:
    """An in-memory lab_alerts table behind the statements the writer sends."""

    def __init__(self, table: dict[_Key, dict[str, Any]]) -> None:
        self.table = table
        self.statements: list[tuple[str, tuple[Any, ...]]] = []
        self.in_transaction = False
        self.published_in_transaction: list[bool] = []

    @asynccontextmanager
    async def transaction(self) -> Any:
        self.in_transaction = True
        try:
            yield
        finally:
            self.in_transaction = False

    async def execute(self, sql: str, *args: Any) -> str:
        self.statements.append((sql, args))
        return "OK"

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.statements.append((sql, args))
        if "FOR UPDATE" in sql:
            row = self.table.get(args[0])
            return [dict(row)] if row is not None else []
        if sql.lstrip().upper().startswith("INSERT INTO OMNINODE_INTERNAL.LAB_ALERTS"):
            columns = sql.split("(", 1)[1].split(")", 1)[0]
            names = [name.strip() for name in columns.split(",")]
            row = dict(zip(names, args, strict=True))
            # The table's own checks (0000_create_lab_alerts.sql).
            assert (row["state"] == "cleared") == (row["cleared_at"] is not None)
            assert row["cleared_at"] is None or row["cleared_at"] >= row["raised_at"]
            assert row["owner"].strip()
            assert row["severity"] in {"SEV1", "SEV2", "SEV3", "SEV4"}
            for name in ("raised_at", "last_transition_at"):
                assert isinstance(row[name], datetime), name
            row["projected_at"] = T0
            self.table[row["alert_id"]] = row
            return [dict(row)]
        raise AssertionError(f"unexpected statement: {sql}")


class _Pool:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    @asynccontextmanager
    async def acquire(self) -> Any:
        yield self._conn


class _Db:
    def __init__(self, conn: _AlertsConn) -> None:
        self.pool = _Pool(conn)


def _writer(
    table: dict[_Key, dict[str, Any]],
) -> tuple[HandlerLabAlertsProjectionRunner, _AlertsConn, list[dict[str, Any]]]:
    writer = HandlerLabAlertsProjectionRunner()
    conn = _AlertsConn(table)
    patch = pytest.MonkeyPatch()
    patch.setattr(writer, "_db", _Db(conn))
    published: list[dict[str, Any]] = []

    async def _capture(exposure: Any, **kwargs: Any) -> bool:
        conn.published_in_transaction.append(conn.in_transaction)
        published.append({"topic": exposure.topic, **kwargs})
        return True

    patch.setattr(writer, "publish_snapshot_delta", _capture)
    return writer, conn, published


def _fold(writer: HandlerLabAlertsProjectionRunner, payload: dict[str, Any]) -> bool:
    meta = MessageMeta(partition=0, offset=3, fallback_id="t", topic=TRANSITION_TOPIC)
    return asyncio.run(writer.project_event(TRANSITION_TOPIC, dict(payload), meta))


def _snapshot(table: dict[_Key, dict[str, Any]]) -> str:
    return json.dumps(table, default=str, sort_keys=True)


def test_a_raised_test_alert_is_written_and_published_to_the_snapshot_topic() -> None:
    table: dict[_Key, dict[str, Any]] = {}
    writer, _conn, published = _writer(table)
    assert _fold(writer, _payload(source="test_alert", subject="dev-lane-probe"))
    (row,) = table.values()
    assert row["state"] == "open"
    assert row["owner"] == "lab-floor"
    assert [p["topic"] for p in published] == [EXPOSURE_TOPIC]
    delta = published[0]
    assert delta["op"] == "upsert"
    assert delta["row"]["alert_id"] == lab_alert_id("test_alert", "dev-lane-probe")
    assert delta["row"]["state"] == "open"
    assert delta["source_topic"] == TRANSITION_TOPIC


def test_a_clear_updates_the_raised_row_in_place() -> None:
    table: dict[_Key, dict[str, Any]] = {}
    writer, _conn, published = _writer(table)
    _fold(writer, _payload())
    _fold(writer, _clear())
    assert len(table) == 1
    (row,) = table.values()
    assert row["state"] == "cleared"
    assert row["cleared_at"] == T0 + timedelta(minutes=5)
    assert [p["row"]["state"] for p in published] == ["open", "cleared"]


def test_replaying_every_transition_in_any_order_changes_no_row() -> None:
    sources = ("dev_tag_heartbeat", "container_restarts", "lab_pass_receipt")
    transitions: list[dict[str, Any]] = []
    for index, source in enumerate(sources):
        start = T0 + timedelta(minutes=index)
        transitions.append(_payload(source=source, raised_at=start))
        transitions.append(_clear(source=source, raised_at=start))
        later = start + timedelta(hours=1)
        transitions.append(_payload(source=source, raised_at=later))
    ordered: dict[_Key, dict[str, Any]] = {}
    writer, _c, _p = _writer(ordered)
    for t in transitions:
        _fold(writer, t)
    assert len(ordered) == len(sources)
    expected = _snapshot(ordered)
    for seed in range(5):
        shuffled = list(transitions) * 2
        random.Random(seed).shuffle(shuffled)
        table: dict[_Key, dict[str, Any]] = {}
        writer, _c, _p = _writer(table)
        for t in shuffled:
            _fold(writer, t)
        assert _snapshot(table) == expected
        assert {r["state"] for r in table.values()} == {"open"}


def test_an_older_transition_writes_and_publishes_nothing() -> None:
    table: dict[_Key, dict[str, Any]] = {}
    writer, _conn, published = _writer(table)
    _fold(writer, _payload())
    _fold(writer, _clear())
    before = _snapshot(table)
    published.clear()
    _fold(writer, _payload())
    assert _snapshot(table) == before
    assert published == []


def test_a_replayed_transition_republishes_the_stored_row_without_writing() -> None:
    table: dict[_Key, dict[str, Any]] = {}
    writer, conn, published = _writer(table)
    _fold(writer, _payload())
    published.clear()
    conn.statements.clear()
    _fold(writer, _payload())
    assert not any("INSERT" in sql.upper() for sql, _ in conn.statements)
    assert [p["row"]["state"] for p in published] == ["open"]


def test_the_key_lock_and_read_come_before_the_write() -> None:
    table: dict[_Key, dict[str, Any]] = {}
    writer, conn, _published = _writer(table)
    _fold(writer, _payload())
    kinds = [
        "lock"
        if "pg_advisory_xact_lock" in sql
        else "read"
        if "FOR UPDATE" in sql
        else "write"
        for sql, _ in conn.statements
    ]
    assert kinds == ["lock", "read", "write"]


def test_the_snapshot_delta_is_published_while_the_key_lock_is_held() -> None:
    table: dict[_Key, dict[str, Any]] = {}
    writer, conn, _published = _writer(table)
    _fold(writer, _payload())
    _fold(writer, _payload())
    assert conn.published_in_transaction == [True, True]


def test_a_malformed_transition_is_refused_and_nothing_is_written() -> None:
    table: dict[_Key, dict[str, Any]] = {}
    writer, conn, published = _writer(table)
    with pytest.raises(ValidationError):
        _fold(writer, _payload(owner=""))
    assert table == {}
    assert published == []
    assert conn.statements == []


# ---- the contract, table and shape document: F5, F6 --------------------------------


def _contract() -> dict[str, Any]:
    return yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))


def test_the_node_subscribes_to_the_transition_topic_and_declares_its_dlq() -> None:
    contract = _contract()
    assert contract["event_bus"]["subscribe_topics"] == [TRANSITION_TOPIC]
    assert contract["event_bus"]["dlq_topics"]
    assert HandlerLabAlertsProjectionRunner().subscribe_topics == [TRANSITION_TOPIC]


def test_the_exposure_is_bus_backed_keyed_on_the_alert_id_and_columns_are_the_tables() -> (
    None
):
    contract = _contract()
    exposures = load_projection_exposures_from_contract(
        contract, str(contract["name"]), CONTRACT_PATH
    )
    assert len(exposures) == 1
    exposure = exposures[0]
    assert exposure.topic == EXPOSURE_TOPIC
    assert exposure.bus_backed
    assert tuple(exposure.key_columns) == ("alert_id",)
    sql = (NODE_DIR / "migrations" / "0000_create_lab_alerts.sql").read_text()
    for column in exposure.columns:
        assert re.search(rf"^\s+{column}\s", sql, re.MULTILINE), column
    for column in (
        "state",
        "severity",
        "owner",
        "raised_at",
        "cleared_at",
        "source",
        "subject",
    ):
        assert column in exposure.columns


def test_runtime_dispatch_resolves_only_the_in_process_writer() -> None:
    handlers = _contract()["handler_routing"]["handlers"]
    assert [h["handler"]["name"] for h in handlers] == ["LabAlertsProjectionWriter"]
    assert LabAlertsProjectionWriter.onex_runtime_inprocess_dispatch is True


def test_the_table_is_declared_in_db_io_and_the_ownership_registry() -> None:
    tables = _contract()["db_io"]["db_tables"]
    assert [t["name"] for t in tables] == ["lab_alerts"]
    registry = (ROOT / "scripts" / "application-relation-ownership.yaml").read_text()
    assert "migrations/0000_create_lab_alerts.sql" in registry


def test_every_column_the_create_declares_is_reasserted_by_an_add_column_guard() -> (
    None
):
    """CREATE TABLE IF NOT EXISTS no-ops against a drifted pre-existing table, so the
    platform's migration shape gate needs each declared column re-asserted by an
    ``ALTER TABLE ... ADD COLUMN IF NOT EXISTS`` in the same file."""
    sql = (NODE_DIR / "migrations" / "0000_create_lab_alerts.sql").read_text()
    body = sql.split("CREATE TABLE IF NOT EXISTS omninode_internal.lab_alerts (", 1)[1]
    body = body.split("\n);", 1)[0]
    declared = [
        match.group(1)
        for line in body.splitlines()
        if (match := re.match(r"^    ([a-z_]+) [A-Z]", line))
        and match.group(1) not in {"PRIMARY", "CONSTRAINT"}
    ]
    assert declared[0] == "alert_id"
    assert len(declared) == 12
    guarded = set(
        re.findall(
            r"ALTER TABLE omninode_internal\.lab_alerts\s+ADD COLUMN IF NOT EXISTS ([a-z_]+) ",
            sql,
        )
    )
    assert set(declared) - guarded == set()


def test_the_node_is_registered_as_an_entry_point() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text()
    assert (
        'node_projection_lab_alerts = "omnimarket.nodes.node_projection_lab_alerts"'
        in pyproject
    )


def test_the_shape_document_states_a_meaning_for_every_exposed_column() -> None:
    contract = _contract()
    exposure = load_projection_exposures_from_contract(
        contract, str(contract["name"]), CONTRACT_PATH
    )[0]
    shape = yaml.safe_load(SHAPE_DOC.read_text(encoding="utf-8"))
    assert shape["read"]["snapshot_topic"] == EXPOSURE_TOPIC
    assert set(shape["fields"]) == set(exposure.columns)
    for column in exposure.columns:
        field = shape["fields"][column]
        assert str(field["meaning"]).strip(), f"{column} has no meaning stated"
        assert str(field["type"]).strip(), f"{column} has no type stated"


def test_the_shape_documents_example_is_a_valid_row() -> None:
    shape = yaml.safe_load(SHAPE_DOC.read_text(encoding="utf-8"))
    example = shape["example"]
    contract = _contract()
    exposure = load_projection_exposures_from_contract(
        contract, str(contract["name"]), CONTRACT_PATH
    )[0]
    assert set(example) == set(exposure.columns)
    assert example["alert_id"] == lab_alert_id(example["source"], example["subject"])
    assert example["state"] in {"open", "cleared"}
    assert (example["state"] == "cleared") == (example["cleared_at"] is not None)
    assert example["severity"] in {"SEV1", "SEV2", "SEV3", "SEV4"}
    for name in ("owner", "detail"):
        assert str(example[name]).strip()
    raised = datetime.fromisoformat(example["raised_at"])
    assert raised.tzinfo is not None
    assert datetime.fromisoformat(example["cleared_at"]) >= raised
    assert EnumLabAlertTransition.RAISED.value == "raised"


SOURCES_DOC = ROOT / "docs" / "reference" / "lab-alert-sources.yaml"
ALARM_CONTRACT = (
    ROOT / "src" / "omnimarket" / "nodes" / "node_lab_alarm" / "contract.yaml"
)


def test_every_declared_alarm_source_is_listed_with_whether_its_alerts_reach_the_bus() -> (
    None
):
    declared = yaml.safe_load(ALARM_CONTRACT.read_text(encoding="utf-8"))[
        "alarm_sources"
    ]
    listed = yaml.safe_load(SOURCES_DOC.read_text(encoding="utf-8"))["sources"]
    assert [entry["condition"] for entry in listed] == [
        entry["condition"] for entry in declared
    ], (
        "a declared alarm source is missing from the list, or the list holds one that is not declared"
    )
    for entry, source in zip(listed, declared, strict=True):
        assert entry["posts_via"] == source["posts_via"], entry["condition"]
        assert isinstance(entry["publishes_transition"], bool), entry["condition"]


def test_a_source_listed_as_publishing_the_transition_declares_the_topic() -> None:
    listed = yaml.safe_load(SOURCES_DOC.read_text(encoding="utf-8"))["sources"]
    publishers = [e["condition"] for e in listed if e["publishes_transition"]]
    transition_topic = _contract()["event_bus"]["subscribe_topics"][0]
    declared_by = [
        path
        for path in (ROOT / "src" / "omnimarket" / "nodes").glob("*/contract.yaml")
        if path.parent.name != "node_projection_lab_alerts"
        and transition_topic in path.read_text(encoding="utf-8")
    ]
    assert bool(publishers) == bool(declared_by), (publishers, declared_by)
