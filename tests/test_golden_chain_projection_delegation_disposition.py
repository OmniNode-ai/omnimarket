"""OMN-20242 rule-7a fold and tenant-safe monotonic writer."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.events.delegation_disposition import (
    TOPIC,
    ModelDelegationDispositionRecorded,
)
from omnimarket.nodes.node_projection_delegation_disposition.handlers import (
    HandlerDelegationDispositionWriter,
    HandlerProjectionDelegationDisposition,
)
from omnimarket.nodes.node_projection_delegation_disposition.models import (
    ModelDelegationDispositionProjectionRequest,
)
from omnimarket.projection.runner import BaseProjectionRunner

pytestmark = pytest.mark.unit
_OUT_TOPIC = "onex.evt.omnimarket.projection-delegation-disposition-applied.v1"  # onex-topic-allow: asserted against the contract
NODE = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_projection_delegation_disposition"
)
T0 = datetime(2026, 9, 30, tzinfo=UTC)


def _event(**updates: Any) -> dict[str, Any]:
    return ModelDelegationDispositionRecorded.build(
        **(
            {
                "tenant_id": UUID("11111111-1111-1111-1111-111111111111"),
                "delegation_correlation_id": UUID(
                    "22222222-2222-2222-2222-222222222222"
                ),
                "caller_lane": "lane-1",
                "disposition": "accepted_as_is",
                "reason_code": "correct_as_is",
                "artifact_kind": "pull_request",
                "artifact_ref": "OmniNode-ai/omnimarket#42",
                "engine": "glm",
                "edit_ratio": None,
                "ticket_id": "OMN-20242",
                "answer_sha256": "a" * 64,
                "recorded_at": T0,
            }
            | updates
        )
    ).model_dump(mode="json")


class _Store:
    def __init__(self) -> None:
        self.rows: dict[tuple[Any, ...], dict[str, Any]] = {}
        self.loop: asyncio.AbstractEventLoop | None = None

    async def connect(self) -> None:
        assert self.loop is None
        self.loop = asyncio.get_running_loop()

    async def close(self) -> None:
        self.loop = None

    async def execute(
        self, query: str, *params: Any, tenant: str | None = None
    ) -> list[dict[str, Any]]:
        assert self.loop is asyncio.get_running_loop()
        assert "ON CONFLICT (tenant_id, delegation_correlation_id)" in query
        assert "(EXCLUDED.recorded_at, EXCLUDED.disposition_id)" in query
        assert (
            "(public.delegation_dispositions.recorded_at, public.delegation_dispositions.disposition_id) "
            "< (EXCLUDED.recorded_at, EXCLUDED.disposition_id)"
        ) in " ".join(query.split())
        assert "RETURNING" in query
        columns = [
            c.strip() for c in query.split("(", 1)[1].split(")", 1)[0].split(",")
        ]
        row = dict(zip(columns, params, strict=True))
        assert isinstance(row["tenant_id"], UUID)
        assert isinstance(row["delegation_correlation_id"], UUID)
        assert isinstance(row["recorded_at"], datetime)
        assert tenant == str(row["tenant_id"])
        key = (row["tenant_id"], row["delegation_correlation_id"])

        def order(r: dict[str, Any]) -> tuple[Any, Any]:
            return r["recorded_at"], r["disposition_id"]

        if key in self.rows and order(self.rows[key]) >= order(row):
            return []
        self.rows[key] = row
        return [{"disposition_id": row["disposition_id"]}]


def _writer() -> tuple[HandlerDelegationDispositionWriter, _Store]:
    writer, store = HandlerDelegationDispositionWriter(), _Store()
    writer._db = store  # type: ignore[assignment]
    return writer, store


def test_fold_is_pure_and_order_independent() -> None:
    events = [
        _event(),
        _event(
            recorded_at=T0 + timedelta(seconds=1),
            disposition="edited",
            reason_code="minor_fix",
            edit_ratio=0.2,
        ),
    ]
    fold = HandlerProjectionDelegationDisposition()
    rows = []
    for ordered in (events, list(reversed(events))):
        writer, store = _writer()
        for event in ordered:
            request = ModelDelegationDispositionProjectionRequest.model_validate(event)
            assert fold.handle(request) == fold.handle(request)
            assert fold.handle(request).rows[0].model_dump(mode="json") == event
            writer.handle(event)
        rows.append(store.rows)
    assert rows[0] == rows[1]
    assert vars(fold) == {}


def test_older_never_replaces_newer() -> None:
    writer, store = _writer()
    writer.handle(_event(recorded_at=T0 + timedelta(seconds=1)))
    before = dict(store.rows)
    writer.handle(_event(disposition="rejected", reason_code="wrong_answer"))
    assert store.rows == before


def test_equal_timestamps_use_uuid_tiebreaker_in_both_arrival_orders() -> None:
    events = [_event(caller_lane=lane) for lane in ("lane-1", "lane-2")]
    winner = max(events, key=lambda e: UUID(e["disposition_id"]))
    for ordered in (events, list(reversed(events))):
        writer, store = _writer()
        for event in ordered:
            writer.handle(event)
        assert next(iter(store.rows.values()))["disposition_id"] == UUID(
            winner["disposition_id"]
        )


def test_redelivered_event_writes_the_same_row() -> None:
    writer, store = _writer()
    value = _event()
    writer.handle(value)
    before = dict(store.rows)
    writer.handle(value)
    assert store.rows == before
    assert len(store.rows) == 1


def test_writer_declares_inprocess_dispatch() -> None:
    assert issubclass(HandlerDelegationDispositionWriter, BaseProjectionRunner)
    assert HandlerDelegationDispositionWriter.onex_runtime_inprocess_dispatch is True
    assert not getattr(
        HandlerProjectionDelegationDisposition, "onex_runtime_inprocess_dispatch", False
    )


def test_writer_row_count_never_zero_for_non_empty_input() -> None:
    writer, store = _writer()
    for value in (_event(), _event(), _event(recorded_at=T0 - timedelta(seconds=1))):
        assert writer.handle(value)["rows_handled"] == 1
    assert store.loop is None


@pytest.mark.parametrize(
    ("disposition", "reason", "ratio"),
    [
        ("accepted_as_is", "correct_as_is", None),
        ("edited", "minor_fix", 0.5),
        ("rejected", "wrong_answer", None),
        ("ignored", "not_needed", None),
    ],
)
def test_each_disposition_is_written(
    disposition: str, reason: str, ratio: float | None
) -> None:
    writer, store = _writer()
    value = _event(disposition=disposition, reason_code=reason, edit_ratio=ratio)
    assert writer.handle(value)["rows_handled"] == 1
    assert next(iter(store.rows.values()))["disposition"] == disposition


def test_tenants_have_separate_rows() -> None:
    writer, store = _writer()
    for tenant in (
        "11111111-1111-1111-1111-111111111111",
        "33333333-3333-3333-3333-333333333333",
    ):
        writer.handle(_event(tenant_id=tenant))
    assert len(store.rows) == 2


def test_runtime_metadata_cannot_replace_producer_time() -> None:
    request = ModelDelegationDispositionProjectionRequest.model_validate(
        _event() | {"_envelope_timestamp": T0 + timedelta(days=1)}
    )
    assert request.recorded_at == T0
    data = _event()
    data.pop("recorded_at")
    with pytest.raises(ValidationError):
        ModelDelegationDispositionProjectionRequest.model_validate(
            data | {"_envelope_timestamp": T0}
        )
    with pytest.raises(ValidationError):
        ModelDelegationDispositionProjectionRequest.model_validate(
            _event() | {"answer": "content"}
        )


def test_runtime_dispatch_metadata_is_not_persisted() -> None:
    writer, store = _writer()
    data = _event() | {
        "_db": object(),
        "_topic": TOPIC,
        "_event_type": "delegation-disposition-recorded",
        "_partition": 2,
        "_offset": 7,
        "_envelope_id": "envelope-1",
        "_envelope_timestamp": T0 + timedelta(days=1),
        "_tenant_id": "33333333-3333-3333-3333-333333333333",
    }
    assert writer.handle(data)["rows_handled"] == 1
    row = next(iter(store.rows.values()))
    assert row["recorded_at"] == T0
    assert str(row["tenant_id"]) == data["tenant_id"]
    assert all(not field.startswith("_") for field in row)
    assert "_db" in data


def test_contract_routes_only_writer_and_declares_relations() -> None:
    contract = yaml.safe_load((NODE / "contract.yaml").read_text())
    assert contract["event_bus"]["subscribe_topics"] == [TOPIC]
    assert _writer()[0].subscribe_topics == [TOPIC]
    assert contract["terminal_event"] == _OUT_TOPIC
    assert contract["event_bus"]["publish_topics"] == [_OUT_TOPIC]
    (route,) = contract["handler_routing"]["handlers"]
    assert route["handler"]["name"] == "HandlerDelegationDispositionWriter"
    tables = {table["name"]: table for table in contract["db_io"]["db_tables"]}
    assert tables["delegation_events"]["access"] == "read"
    assert tables["delegation_dispositions"]["access"] == "read_write"
    assert set(tables) == {"delegation_events", "delegation_dispositions"}


def test_migrations_declare_tenant_isolation() -> None:
    migrations = NODE / "migrations"
    ddl = (migrations / "0000_create_delegation_dispositions.sql").read_text()
    assert "PRIMARY KEY (tenant_id, delegation_correlation_id)" in ddl
    assert "ENABLE ROW LEVEL SECURITY" in ddl
    force = (migrations / "0002_force_rls_delegation_dispositions.sql").read_text()
    assert "FORCE ROW LEVEL SECURITY" in force
    assert "CREATE POLICY tenant_isolation" in force
    assert (
        "WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid)" in force
    )
    grants = (
        migrations / "0001_grant_tenant_projection_writer_delegation_dispositions.sql"
    ).read_text()
    assert "TO tenant_projection_writer" in grants
    assert len(list(migrations.glob("*.sql"))) == 3
    assert all(
        "delegation_events" not in migration.read_text()
        for migration in migrations.glob("*.sql")
    )
