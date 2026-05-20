"""Golden chain tests for node_projection_savings."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_projection_savings.handlers.handler_projection_savings import (
    HandlerProjectionSavings,
    ModelSavingsEstimatedEvent,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

HANDLER = HandlerProjectionSavings()


class TestSavingsProjection:
    def test_project_single_event(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = ModelSavingsEstimatedEvent(
            event_timestamp=datetime(2026, 4, 29, 12, 0, tzinfo=UTC),
            session_id="sess-001",
            model_local="qwen3-coder-30b",
            model_cloud_baseline="claude-opus-4",
            local_cost_usd=Decimal("0.000000"),
            cloud_cost_usd=Decimal("12.340000"),
            savings_usd=Decimal("12.340000"),
            repo_name="omniclaude",
            machine_id="m-201",
        )
        result = HANDLER.project(event, db)
        assert result.rows_upserted == 1
        rows = db.query("savings_estimates")
        assert len(rows) == 1
        assert rows[0] == {
            "event_timestamp": "2026-04-29T12:00:00+00:00",
            "session_id": "sess-001",
            "model_local": "qwen3-coder-30b",
            "model_cloud_baseline": "claude-opus-4",
            "local_cost_usd": Decimal("0.000000"),
            "cloud_cost_usd": Decimal("12.340000"),
            "savings_usd": Decimal("12.340000"),
            "repo_name": "omniclaude",
            "machine_id": "m-201",
            "created_at": rows[0]["created_at"],
            "updated_at": rows[0]["updated_at"],
        }

    def test_project_normalizes_event_timestamp_to_utc_identity(self) -> None:
        db = InmemoryDatabaseAdapter()
        offset_tz = timezone(timedelta(hours=-4))
        HANDLER.project(
            ModelSavingsEstimatedEvent(
                event_timestamp=datetime(2026, 4, 29, 8, 0, tzinfo=offset_tz),
                session_id="sess-offset",
                model_local="qwen3-coder-30b",
                model_cloud_baseline="claude-opus-4",
                local_cost_usd=Decimal("1.000000"),
                cloud_cost_usd=Decimal("2.000000"),
                savings_usd=Decimal("1.000000"),
            ),
            db,
        )
        HANDLER.project(
            ModelSavingsEstimatedEvent(
                event_timestamp=datetime(2026, 4, 29, 12, 0, tzinfo=UTC),
                session_id="sess-offset",
                model_local="qwen3-coder-30b",
                model_cloud_baseline="claude-opus-4",
                local_cost_usd=Decimal("0.500000"),
                cloud_cost_usd=Decimal("2.000000"),
                savings_usd=Decimal("1.500000"),
            ),
            db,
        )

        rows = db.query("savings_estimates")
        assert len(rows) == 1
        assert rows[0]["event_timestamp"] == "2026-04-29T12:00:00+00:00"
        assert rows[0]["savings_usd"] == Decimal("1.500000")

    def test_handle_strips_transport_metadata(self) -> None:
        db = InmemoryDatabaseAdapter()
        result = HANDLER.handle(
            {
                "_db": db,
                "_topic": "onex.evt.omnibase-infra.savings-estimated.v1",
                "_partition": 0,
                "_offset": 1,
                "rows": [],
                "event_landed": True,
                "latency_ms": 12,
                "event_timestamp": "2026-04-29T12:00:00Z",
                "session_id": "sess-transport",
                "model_local": "qwen3-coder-30b",
                "model_cloud_baseline": "claude-opus-4",
                "local_cost_usd": "0.100000",
                "cloud_cost_usd": "0.300000",
                "savings_usd": "0.200000",
            }
        )
        assert result["rows_upserted"] == 1

    def test_inmemory_upsert_rejects_missing_conflict_keys(self) -> None:
        db = InmemoryDatabaseAdapter()
        with pytest.raises(KeyError):
            db.upsert(
                "savings_estimates", "session_id,event_timestamp", {"session_id": "s1"}
            )

    def test_inmemory_upsert_rejects_empty_conflict_key(self) -> None:
        db = InmemoryDatabaseAdapter()
        with pytest.raises(ValueError, match="conflict_key must contain"):
            db.upsert("savings_estimates", " , ", {"session_id": "s1"})

    def test_upsert_by_session_timestamp_local_and_cloud_baseline(self) -> None:
        db = InmemoryDatabaseAdapter()
        HANDLER.project(
            ModelSavingsEstimatedEvent(
                event_timestamp=datetime(2026, 4, 29, 12, 0, tzinfo=UTC),
                session_id="s1",
                model_local="qwen3-coder-30b",
                model_cloud_baseline="claude-opus-4",
                local_cost_usd=Decimal("1.000000"),
                cloud_cost_usd=Decimal("2.000000"),
                savings_usd=Decimal("1.000000"),
            ),
            db,
        )
        HANDLER.project(
            ModelSavingsEstimatedEvent(
                event_timestamp=datetime(2026, 4, 29, 12, 0, tzinfo=UTC),
                session_id="s1",
                model_local="qwen3-coder-30b",
                model_cloud_baseline="claude-opus-4",
                local_cost_usd=Decimal("0.500000"),
                cloud_cost_usd=Decimal("2.000000"),
                savings_usd=Decimal("1.500000"),
            ),
            db,
        )
        rows = db.query("savings_estimates")
        assert len(rows) == 1
        assert rows[0]["savings_usd"] == Decimal("1.500000")

    def test_upsert_refreshes_updated_at(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import omnimarket.nodes.node_projection_savings.handlers.handler_projection_savings as module

        timestamps = [
            datetime(2026, 4, 29, 12, 0, tzinfo=UTC),
            datetime(2026, 4, 29, 12, 5, tzinfo=UTC),
        ]

        class FakeDateTime:
            @classmethod
            def now(cls, tz: timezone | None = None) -> datetime:
                value = timestamps.pop(0)
                if tz is not None:
                    return value.astimezone(tz)
                return value

        monkeypatch.setattr(module, "datetime", FakeDateTime)
        db = InmemoryDatabaseAdapter()
        event = ModelSavingsEstimatedEvent(
            event_timestamp=datetime(2026, 4, 29, 12, 0, tzinfo=UTC),
            session_id="s1",
            model_local="qwen3-coder-30b",
            model_cloud_baseline="claude-opus-4",
            local_cost_usd=Decimal("1.000000"),
            cloud_cost_usd=Decimal("2.000000"),
            savings_usd=Decimal("1.000000"),
        )

        HANDLER.project(event, db)
        first_row = db.query("savings_estimates")[0]
        HANDLER.project(
            event.model_copy(
                update={
                    "local_cost_usd": Decimal("0.500000"),
                    "savings_usd": Decimal("1.500000"),
                }
            ),
            db,
        )

        rows = db.query("savings_estimates")
        assert len(rows) == 1
        assert first_row["updated_at"] == "2026-04-29T12:00:00+00:00"
        assert rows[0]["updated_at"] == "2026-04-29T12:05:00+00:00"

    def test_project_batch(self) -> None:
        db = InmemoryDatabaseAdapter()
        events = [
            ModelSavingsEstimatedEvent(
                event_timestamp=datetime(2026, 4, 29, 12, i, tzinfo=UTC),
                session_id=f"sess-{i:03d}",
                model_local="qwen3-coder-30b",
                model_cloud_baseline="claude-opus-4",
                local_cost_usd=Decimal("0.000000"),
                cloud_cost_usd=Decimal(f"{i}.000000"),
                savings_usd=Decimal(f"{i}.000000"),
            )
            for i in range(4)
        ]
        result = HANDLER.project_batch(events, db)
        assert result.rows_upserted == 4

    def test_event_bus_wiring(self) -> None:
        contract_path = "src/omnimarket/nodes/node_projection_savings/contract.yaml"
        with open(contract_path) as f:
            contract = yaml.safe_load(f)
        assert (
            "onex.evt.omnibase-infra.savings-estimated.v1"
            in contract["event_bus"]["subscribe_topics"]
        )
        assert (
            "onex.evt.omnimarket.delegate-skill-completed.v1"
            in contract["event_bus"]["subscribe_topics"]
        )
        assert (
            "onex.evt.omnimarket.delegate-skill-failed.v1"
            in contract["event_bus"]["subscribe_topics"]
        )
        assert (
            contract["event_bus"]["consumer_group"]
            == "local.omnibase_infra.node_projection_savings.consume.v1"
        )

    def test_migration_declares_handler_schema(self) -> None:
        migration_path = Path(
            "src/omnimarket/nodes/node_projection_savings/migrations/"
            "074_create_savings_estimates.sql"
        )
        migration = migration_path.read_text()
        assert "CREATE TABLE IF NOT EXISTS savings_estimates" in migration
        assert "event_timestamp TIMESTAMPTZ NOT NULL" in migration
        assert "session_id TEXT NOT NULL" in migration
        assert "model_local TEXT NOT NULL" in migration
        assert "model_cloud_baseline TEXT NOT NULL" in migration
        assert "ux_savings_estimates_identity" in migration
        assert "trg_savings_estimates_updated_at" in migration
        assert "NEW.updated_at = NOW()" in migration

    def test_fixture_replay_matches_golden_checksums(self) -> None:
        db = InmemoryDatabaseAdapter()
        fixture_path = Path(
            "tests/fixtures/cost_observability/task-9-savings.fixtures.jsonl"
        )
        golden_path = Path(
            "tests/fixtures/cost_observability/task-9-savings.golden.json"
        )

        for line in fixture_path.read_text().splitlines():
            event = ModelSavingsEstimatedEvent(**json.loads(line))
            HANDLER.project(event, db)

        rows = sorted(
            db.query("savings_estimates"),
            key=lambda row: (
                str(row["session_id"]),
                str(row["event_timestamp"]),
                str(row["model_local"]),
                str(row["model_cloud_baseline"]),
            ),
        )
        checksums = [_row_checksum(row) for row in rows]
        assert json.loads(golden_path.read_text()) == {
            "row_count": len(rows),
            "checksums": checksums,
        }


def _row_checksum(row: dict[str, object]) -> str:
    stable = {
        key: str(value)
        for key, value in row.items()
        if key not in {"created_at", "updated_at"}
    }
    encoded = json.dumps(stable, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
