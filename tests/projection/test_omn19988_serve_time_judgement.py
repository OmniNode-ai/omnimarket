# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Serve recorded health under total silence, through real contract discovery.

No observer or writer is run during the aged reads. The SQLite source and
portable writer are real; only request time and deliberate fault seams vary.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.handlers import handler_local_environment_judgement as judgement
from omnimarket.handlers import handler_local_environment_store as store
from omnimarket.models.model_local_environment_status import ModelLocalEnvironmentStatus
from omnimarket.models.model_local_service_health import ModelLocalServiceHealth
from omnimarket.nodes.node_local_dashboard_serve_effect.handlers.handler_native_environment_report import (
    build_native_report,
)
from omnimarket.nodes.node_projection_read_effect.ports.sqlite_row_source import (
    SqliteTableRowSource,
)
from omnimarket.projection import read_page
from omnimarket.projection.discovery import (
    build_projection_topic_map,
    load_projection_exposures_from_contract,
)
from omnimarket.projection.models import ProjectionStatus, ProjectionTableConfig
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter
from omnimarket.projection.table_reader import ProjectionReadError
from omnimarket.projection.tenant_isolation import TenantContextMissingError

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
NODES = ROOT / "src/omnimarket/nodes"
CONTRACT = NODES / "node_projection_local_environment/contract.yaml"
ALERT_CONTRACT = NODES / "node_local_alert_compute/contract.yaml"
TOPIC = "onex.snapshot.projection.local-environment.v1"
TENANT = "6c24401e-0000-4000-8000-00000000000a"
T0 = datetime(2026, 10, 18, 6, tzinfo=UTC)
SERVICES = ("postgres", "redpanda", "valkey")
JUDGE = "omnimarket.handlers.handler_local_environment_judgement.judge_local_services"
DERIVED = {"verdict", "verdict_reason", "alert_code", "alert_severity", "alert_since"}


@dataclass
class _Clock:
    at: datetime = T0

    def advance_to(self, seconds: int) -> None:
        self.at = T0 + timedelta(seconds=seconds)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    value = _Clock()

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> datetime:
            return value.at.astimezone(tz) if tz else value.at.replace(tzinfo=None)

    # The store stamp and the read envelope are deterministic. No sleep or
    # post-seed report is needed to cross the freshness deadline.
    monkeypatch.setattr(read_page, "datetime", FrozenDatetime)
    monkeypatch.setattr(store, "datetime", FrozenDatetime)
    return value


@pytest.fixture(scope="module")
def discovered_topics() -> dict[str, ProjectionTableConfig]:
    # The normal installed-contract walk, not a test-created config object.
    return build_projection_topic_map()


def _contract() -> dict[str, Any]:
    document: dict[str, Any] = yaml.safe_load(CONTRACT.read_text())
    return document


def _declared_topics(document: dict[str, Any]) -> dict[str, ProjectionTableConfig]:
    return {
        cfg.topic: cfg
        for cfg in load_projection_exposures_from_contract(
            document, str(document["name"]), CONTRACT
        )
    }


def _seed(path: Path, services: tuple[str, ...] = SERVICES) -> None:
    db = SqliteDatabaseAdapter(path)
    for service in services:
        report = ModelLocalEnvironmentStatus.model_validate(
            {
                "tenant_id": TENANT,
                "mode": "compose",
                "service": service,
                "observed_state": "running",
                "reason": f"{service} running",
                "observed_at": T0,
                "versions": {"omnimarket": "proof"},
            }
        )
        store.apply_local_environment_status(
            report, db, judgement.load_local_alert_rules()
        )


def _stored(path: Path) -> list[dict[str, Any]]:
    rows = SqliteDatabaseAdapter(path).query("local_environment", {"tenant_id": TENANT})
    return sorted(rows, key=lambda row: (row["mode"], row["service"]))


def _wire_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = copy.deepcopy(rows)
    for row in result:
        if isinstance(row["versions"], str):
            row["versions"] = json.loads(row["versions"])
    return result


async def _read(
    path: Path,
    topics: dict[str, ProjectionTableConfig],
    *,
    topic: str = TOPIC,
    source: SqliteTableRowSource | None = None,
) -> read_page.ProjectionPage:
    return await read_page.read_projection_page(
        topic,
        topic_map=topics,
        source=source or SqliteTableRowSource(path),
        tenant=TENANT,
    )


def _assert_all_stale(page: read_page.ProjectionPage, since_seconds: int = 60) -> None:
    assert page.status_code == 200, page.body
    rows = page.body["rows"]
    assert rows, "the seeded services must be present"
    assert {row["verdict"] for row in rows} == {"stale"}, (
        "all served rows must be STALE under total report silence"
    )
    assert {row["alert_code"] for row in rows} == {"HEALTH_UNKNOWN"}
    assert {row["alert_severity"] for row in rows} == {"warning"}
    assert {row["alert_since"] for row in rows} == {
        (T0 + timedelta(seconds=since_seconds)).isoformat()
    }


def test_actual_contract_declaration_survives_production_discovery(
    discovered_topics: dict[str, ProjectionTableConfig],
) -> None:
    document = _contract()
    assert document["projection_api"]["serve_time_judgement"] == JUDGE
    # Discovery resolves the physical read schema; the direct writer parser
    # deliberately leaves that serving-only field unset.
    physical_schema = next(
        table["schema"]
        for table in document["db_io"]["db_tables"]
        if table["name"] == document["projection_api"]["table"]
    )
    direct = _declared_topics(document)[TOPIC].model_copy(
        update={"relation_schema": physical_schema}
    )
    discovered = discovered_topics[TOPIC]
    assert isinstance(discovered, ProjectionTableConfig)
    assert discovered == direct
    assert discovered.serve_time_judgement == JUDGE
    assert discovered.expected_event_interval_seconds == 15


@pytest.mark.asyncio
@pytest.mark.parametrize("seconds", [59, 60, 61])
async def test_served_silence_deadline_without_another_report(
    tmp_path: Path,
    clock: _Clock,
    discovered_topics: dict[str, ProjectionTableConfig],
    seconds: int,
) -> None:
    path = tmp_path / "environment.sqlite"
    _seed(path)
    before = copy.deepcopy(_stored(path))
    clock.advance_to(seconds)
    page = await _read(path, discovered_topics)
    assert page.status_code == 200, page.body
    assert {row["service"] for row in page.body["rows"]} == set(SERVICES)
    if seconds <= 60:
        assert {row["verdict"] for row in page.body["rows"]} == {"running"}
        assert {row["alert_code"] for row in page.body["rows"]} == {None}
    else:
        _assert_all_stale(page)
    assert _stored(path) == before, "a served judgement must not persist any change"
    originals = {row["service"]: row for row in _wire_rows(before)}
    for row in page.body["rows"]:
        original = originals[row["service"]]
        assert row.keys() == original.keys()
        assert {key: value for key, value in row.items() if key not in DERIVED} == {
            key: value for key, value in original.items() if key not in DERIVED
        }, (
            "identity, observed reason, report id, timestamps and cursor must stay intact"
        )


@pytest.mark.asyncio
async def test_served_judgement_does_not_mutate_cached_source_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clock: _Clock,
    discovered_topics: dict[str, ProjectionTableConfig],
) -> None:
    path = tmp_path / "environment.sqlite"
    _seed(path)
    source = SqliteTableRowSource(path)
    cached = await source.rows(
        discovered_topics[TOPIC],
        order_spec=(("mode", "ASC", None), ("service", "ASC", None)),
        tenant_id=TENANT,
    )
    before = copy.deepcopy(cached)
    calls: list[None] = []

    async def cached_rows(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        calls.append(None)
        return cached

    async def cached_latest_event_at(
        cfg: ProjectionTableConfig,
        *,
        tenant_id: str | None,
        window_rows: list[dict[str, Any]] | None,
    ) -> datetime | None:
        # The SQLite adapter normally asks rows() again for a cursor walk's
        # freshness. This fixture owns both reads of the same cached window.
        return max(datetime.fromisoformat(row[cfg.freshness_column]) for row in cached)

    monkeypatch.setattr(source, "rows", cached_rows)
    monkeypatch.setattr(source, "latest_event_at", cached_latest_event_at)
    clock.advance_to(61)
    page = await _read(path, discovered_topics, source=source)
    _assert_all_stale(page)
    assert calls == [None], "the cached source must actually serve this read"
    assert cached == before, (
        "serve-time judgement must copy cached rows before changing them"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("seconds", "expected"), [(14, "fresh"), (15, "idle"), (29, "idle"), (30, "stale")]
)
async def test_envelope_cadence_is_distinct_from_row_deadline(
    tmp_path: Path,
    clock: _Clock,
    discovered_topics: dict[str, ProjectionTableConfig],
    seconds: int,
    expected: str,
) -> None:
    path = tmp_path / "environment.sqlite"
    _seed(path)
    clock.advance_to(seconds)
    page = await _read(path, discovered_topics)
    assert page.status_code == 200, page.body
    assert page.body["generated_at"] == clock.at.isoformat()
    assert page.body["data_freshness"] == expected
    assert {row["verdict"] for row in page.body["rows"]} == {"running"}


@pytest.mark.asyncio
async def test_changed_alert_contract_moves_the_served_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clock: _Clock,
    discovered_topics: dict[str, ProjectionTableConfig],
) -> None:
    path = tmp_path / "environment.sqlite"
    _seed(path)
    document = yaml.safe_load(ALERT_CONTRACT.read_text())
    document["config"]["local_alert"]["freshness_deadline_seconds"] = 90
    resource = tmp_path / "alert-resource"
    resource.mkdir()
    (resource / "contract.yaml").write_text(yaml.safe_dump(document))
    original_files = judgement.files

    def resources(package: str) -> Any:
        if package == "omnimarket.nodes.node_local_alert_compute":
            return resource
        return original_files(package)

    monkeypatch.setattr(judgement, "files", resources)
    assert judgement.load_local_alert_rules().freshness_deadline_seconds == 90
    before = copy.deepcopy(_stored(path))
    for seconds in (61, 89, 90):
        clock.advance_to(seconds)
        page = await _read(path, discovered_topics)
        assert page.status_code == 200, page.body
        assert {row["verdict"] for row in page.body["rows"]} == {"running"}
    clock.advance_to(91)
    _assert_all_stale(await _read(path, discovered_topics), since_seconds=90)
    assert _stored(path) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["missing", "raising"])
async def test_declared_judgement_failure_refuses_without_raw_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clock: _Clock,
    fault: str,
) -> None:
    path = tmp_path / "environment.sqlite"
    _seed(path)
    document = _contract()
    calls: list[datetime] = []

    def raising(services: Any, observed_at: datetime, rules: Any) -> Any:
        calls.append(observed_at)
        raise RuntimeError("judgement-error-private-detail-sentinel")

    if fault == "missing":
        document["projection_api"]["serve_time_judgement"] = (
            "omnimarket.handlers.handler_local_environment_judgement.missing_omn19988_judge"
        )
    else:
        document["projection_api"]["serve_time_judgement"] = JUDGE
        monkeypatch.setattr(judgement, "judge_local_services", raising)
    topics = _declared_topics(document)
    assert TOPIC in topics, (
        "a declared callable is resolved by the read, not silently dropped"
    )
    clock.advance_to(61)
    page = await _read(path, topics)
    assert page.status_code == 503, page.body
    assert page.body["error"] == "serve_time_judgement_failed"
    assert not page.body.get("rows"), "failure must not expose raw healthy rows"
    assert "judgement-error-private-detail-sentinel" not in json.dumps(page.body)
    if fault == "raising":
        assert calls == [clock.at], "the declared callable must actually be invoked"


@pytest.mark.asyncio
async def test_undeclared_judgement_keeps_the_complete_page_identical(
    tmp_path: Path,
    clock: _Clock,
    record_property: Callable[[str, object], None],
) -> None:
    path = tmp_path / "environment.sqlite"
    _seed(path)
    before = copy.deepcopy(_stored(path))
    document = _contract()
    document["projection_api"].pop("serve_time_judgement", None)
    # This field predates the change. Hold it constant in the base/head inert control.
    document["projection_api"]["expected_event_interval_seconds"] = 15
    topics = _declared_topics(document)
    clock.advance_to(61)
    page = await _read(path, topics)
    stamp = T0.isoformat()
    expected = {
        "topic": TOPIC,
        "projection_version": "1.0.0",
        "generated_at": clock.at.isoformat(),
        "data_freshness": "stale",
        "ordering": "mode ASC, service ASC",
        "row_limit": 500,
        "latest_event_at": stamp,
        "latest_projection_updated_at": stamp,
        "row_count": len(before),
        "next_cursor": None,
        "page_selection": "cursor",
        "truncated": False,
        "rows": _wire_rows(before),
        "backing": "sqlite_table",
        "staleness": {
            "stale": False,
            "source": "sqlite_table",
            "lag_records": None,
            "applied_offset": None,
            "end_offset": None,
            "partitions_measured": 0,
            "last_applied_event_at": stamp,
            "dropped_since_apply": 0,
            "dropped_total": 0,
            "last_dropped_event_at": None,
        },
        "tenant": TENANT,
    }
    assert page.status_code == 200, page.body
    actual_bytes = json.dumps(page.body, sort_keys=True, separators=(",", ":")).encode()
    expected_bytes = json.dumps(
        expected, sort_keys=True, separators=(",", ":")
    ).encode()
    assert actual_bytes == expected_bytes
    record_property("undeclared_page_sha256", hashlib.sha256(actual_bytes).hexdigest())
    assert _stored(path) == before


@pytest.mark.asyncio
async def test_stored_time_fault_is_rejected_by_the_silence_assertion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clock: _Clock,
    discovered_topics: dict[str, ProjectionTableConfig],
) -> None:
    path = tmp_path / "environment.sqlite"
    _seed(path)
    original = judgement.judge_local_services
    calls: list[datetime] = []

    def stored_time(
        services: tuple[ModelLocalServiceHealth, ...], observed_at: datetime, rules: Any
    ) -> Any:
        calls.append(observed_at)
        return original(services, max(service.last_seen for service in services), rules)

    monkeypatch.setattr(judgement, "judge_local_services", stored_time)
    clock.advance_to(61)
    page = await _read(path, discovered_topics)
    assert calls == [clock.at], "the wrong-clock fault must actually reach judgement"
    with pytest.raises(AssertionError, match="all served rows must be STALE"):
        _assert_all_stale(page)


@pytest.mark.asyncio
async def test_native_heartbeat_keeps_the_row_live_then_silence_ages_it(
    tmp_path: Path,
    clock: _Clock,
    discovered_topics: dict[str, ProjectionTableConfig],
) -> None:
    path = tmp_path / "environment.sqlite"
    native_contract = yaml.safe_load(
        (NODES / "node_local_dashboard_serve_effect/contract.yaml").read_text()
    )
    interval = native_contract["config"]["local_environment_report"][
        "heartbeat_seconds"
    ]
    assert interval == 15
    db = SqliteDatabaseAdapter(path)
    for seconds in range(0, 61, interval):
        clock.advance_to(seconds)
        report = build_native_report(
            tenant_id=TENANT,
            store_location=path,
            token_minted_at=T0,
            observed_at=clock.at,
            exposure_count=len(discovered_topics),
            versions={"omnimarket": "proof"},
        )
        store.apply_local_environment_status(
            report, db, judgement.load_local_alert_rules()
        )
        clock.advance_to(seconds + interval - 1)
        page = await _read(path, discovered_topics)
        assert page.status_code == 200, page.body
        assert [(row["mode"], row["verdict"]) for row in page.body["rows"]] == [
            ("native", "running")
        ]
    before = copy.deepcopy(_stored(path))
    clock.advance_to(120)  # Exactly 60 seconds after the final heartbeat.
    assert (await _read(path, discovered_topics)).body["rows"][0][
        "verdict"
    ] == "running"
    clock.advance_to(121)
    _assert_all_stale(await _read(path, discovered_topics), since_seconds=120)
    assert _stored(path) == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "refusal", ["unknown", "ambiguous", "degraded", "tenant", "source"]
)
async def test_existing_read_refusals_precede_serve_time_judgement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clock: _Clock,
    discovered_topics: dict[str, ProjectionTableConfig],
    refusal: str,
) -> None:
    path = tmp_path / "environment.sqlite"
    _seed(path)
    topics = dict(discovered_topics)
    topic = TOPIC
    source = SqliteTableRowSource(path)

    def forbidden_judgement(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("an existing read refusal must precede row judgement")

    monkeypatch.setattr(judgement, "judge_local_services", forbidden_judgement)
    if refusal == "unknown":
        topic = "onex.snapshot.projection.missing-omn19988.v1"
        expected_status, expected_error = 404, "unknown_topic"
    elif refusal == "ambiguous":
        topic = "omn19988-ambiguous-alias"
        first = _contract()
        first["projection_api"]["route_aliases"] = [topic]
        second = copy.deepcopy(first)
        second["projection_api"]["topic"] = (
            "onex.snapshot.projection.second-omn19988.v1"
        )
        topics = {**_declared_topics(first), **_declared_topics(second)}
        expected_status, expected_error = 409, "ambiguous_projection_alias"
    elif refusal == "degraded":
        topics[TOPIC] = topics[TOPIC].model_copy(
            update={
                "status": ProjectionStatus.DEGRADED,
                "degraded_reason": "proof-refusal",
            }
        )
        expected_status, expected_error = 503, None
    elif refusal == "tenant":

        def missing_tenant(*args: Any, **kwargs: Any) -> str:
            raise TenantContextMissingError("proof missing tenant")

        monkeypatch.setattr(read_page, "resolve_serving_tenant", missing_tenant)
        expected_status, expected_error = 422, "tenant_context_unresolved"
    else:

        async def unavailable(*args: Any, **kwargs: Any) -> Any:
            raise ProjectionReadError("source_unavailable", "proof source refusal")

        monkeypatch.setattr(source, "rows", unavailable)
        expected_status, expected_error = 503, "source_unavailable"
    page = await _read(path, topics, topic=topic, source=source)
    assert page.status_code == expected_status, page.body
    if expected_error is not None:
        assert page.body["error"] == expected_error
    else:
        assert page.body == {"status": "degraded", "reason": "proof-refusal"}
    assert not page.body.get("rows")
