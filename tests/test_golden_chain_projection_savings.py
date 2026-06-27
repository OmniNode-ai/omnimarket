"""Golden chain tests for node_projection_savings."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
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
from omnimarket.projection.validation import (
    validate_projection_materialization_contracts,
)
from tests.constants import MODEL_CLAUDE_OPUS_4_6, MODEL_QWEN3_CODER_30B

HANDLER = HandlerProjectionSavings()
_DELEGATE_SKILL_TEST_MODEL = "test-model-local"


class TestSavingsProjection:
    def test_project_single_event(self) -> None:
        db = InmemoryDatabaseAdapter()
        event = ModelSavingsEstimatedEvent(
            event_timestamp=datetime(2026, 4, 29, 12, 0, tzinfo=UTC),
            session_id="sess-001",
            model_local=MODEL_QWEN3_CODER_30B,
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
            "model_local": MODEL_QWEN3_CODER_30B,
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
                model_local=MODEL_QWEN3_CODER_30B,
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
                model_local=MODEL_QWEN3_CODER_30B,
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
                "model_local": MODEL_QWEN3_CODER_30B,
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
                model_local=MODEL_QWEN3_CODER_30B,
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
                model_local=MODEL_QWEN3_CODER_30B,
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
            model_local=MODEL_QWEN3_CODER_30B,
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
                model_local=MODEL_QWEN3_CODER_30B,
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
            contract["handler"]["module"]
            == "omnimarket.nodes.node_projection_savings.handlers.handler_projection_savings"
        )
        assert contract["handler"]["class"] == "HandlerProjectionSavings"
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

        repair_migration = Path(
            "src/omnimarket/nodes/node_projection_savings/migrations/"
            "075_add_savings_estimates_updated_at.sql"
        ).read_text()
        assert "ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ" in repair_migration

        overview_migration = Path(
            "src/omnimarket/nodes/node_projection_savings/migrations/"
            "077_create_cost_savings_overview_projection_view.sql"
        ).read_text()
        assert (
            "CREATE OR REPLACE VIEW projection_cost_savings_overview"
            in overview_migration
        )

    def test_sync_handler_projects_delegate_skill_terminal_savings(self) -> None:
        db = InmemoryDatabaseAdapter()
        handler = HandlerProjectionSavings()
        payload: dict[str, object] = {
            "_db": db,
            "_event_type": "delegate-skill-completed",
            "status": "completed",
            "correlation_id": "f9243395-5cb6-4036-8ffb-39dd25547413",
            "task_type": "document",
            "provider": "local-qwen",
            "model_name": _DELEGATE_SKILL_TEST_MODEL,
            "quality_gate_passed": True,
            "metrics": {
                "input_tokens": 81,
                "output_tokens": 384,
                "total_tokens": 465,
                "cost_usd": 0.0,
                "cost_savings_usd": 0.006003,
                "latency_ms": 900,
            },
        }

        result = handler.handle(payload)

        assert result["rows_upserted"] == 1
        row = db.query("savings_estimates")[0]
        assert row["session_id"] == "f9243395-5cb6-4036-8ffb-39dd25547413"
        assert row["model_cloud_baseline"] == MODEL_CLAUDE_OPUS_4_6
        assert row["savings_usd"] == Decimal("0.006003")

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


class TestProjectionSavingsContractConfig:
    """OMN-12761: Assert that the savings contract routes db_io to omnidash_analytics.

    Root cause: contract.yaml had database: omnibase_infra while the
    savings_estimates table (with updated_at) lives in omnidash_analytics.
    Every sibling projection uses omnidash_analytics; this test gates regression.
    """

    def test_db_io_database_is_omnidash_analytics(self) -> None:
        contract_path = Path(
            "src/omnimarket/nodes/node_projection_savings/contract.yaml"
        )
        with contract_path.open() as f:
            contract = yaml.safe_load(f)
        tables = contract["db_io"]["db_tables"]
        savings_table = next(t for t in tables if t["name"] == "savings_estimates")
        assert savings_table["database"] == "omnidash_analytics", (
            "savings_estimates must target omnidash_analytics (not omnibase_infra); "
            "migration 075 applied updated_at to omnidash_analytics only"
        )


_CONTRACT_PATH = Path("src/omnimarket/nodes/node_projection_savings/contract.yaml")
_SERIES_MIGRATION = Path(
    "src/omnimarket/nodes/node_projection_savings/migrations/"
    "078_create_delegation_savings_series_projection_view.sql"
)
_SERIES_TOPIC = "onex.snapshot.projection.delegation.savings-series.v1"
_SERIES_TABLE = "projection_delegation_savings_series"
_SERIES_COLUMNS = [
    "bucket",
    "actual_cost_usd",
    "baseline_cost_usd",
    "savings_usd",
    "task_count",
]


@dataclass(frozen=True)
class _ContractStub:
    name: str
    contract_path: Path


@dataclass(frozen=True)
class _ManifestStub:
    contracts: tuple[_ContractStub, ...]


def _series_exposure() -> dict[str, object]:
    contract = yaml.safe_load(_CONTRACT_PATH.read_text())
    for exposure in contract["projection_api"]["exposures"]:
        if exposure["topic"] == _SERIES_TOPIC:
            return exposure
    raise AssertionError(f"No projection_api exposure declared for {_SERIES_TOPIC!r}")


@pytest.mark.unit
class TestDelegationSavingsSeriesProjection:
    """OMN-13648 (G1): time-bucketed savings-over-time series exposure."""

    def test_migration_078_creates_series_view(self) -> None:
        sql = _SERIES_MIGRATION.read_text()
        # The view is a single CREATE OR REPLACE VIEW over the bus-fed source.
        assert f"CREATE OR REPLACE VIEW {_SERIES_TABLE}" in sql
        # Re-derives the same UNION source as migration 076 (does NOT depend on
        # 076's internal CTEs): both base relations are read directly.
        assert "FROM savings_estimates" in sql
        assert "FROM delegation_events" in sql
        assert "combined_sessions AS" in sql
        # One row per UTC day with SUM/COUNT aggregates.
        assert "date_trunc('day', created_at) AS bucket" in sql
        assert "SUM(local_cost_usd)" in sql
        assert "SUM(cloud_cost_usd)" in sql
        assert "SUM(savings_usd)" in sql
        assert "COUNT(*)::int AS task_count" in sql
        assert "GROUP BY 1" in sql
        assert "ORDER BY 1" in sql
        # Negative assertions run against executable SQL only (strip -- comments
        # so the explanatory header doesn't satisfy the checks).
        sql_body = "\n".join(line.split("--", 1)[0] for line in sql.splitlines())
        # Aggregates over ALL sessions: the LIMIT 500 cap from 076 must NOT leak
        # into the series view.
        assert "LIMIT 500" not in sql_body
        # Tier-mix columns are deferred to OMN-13649; they must not ship here.
        for tier_col in ("local_pct", "cheap_pct", "prem_pct"):
            assert tier_col not in sql_body

    def test_contract_exposes_savings_series(self) -> None:
        exposure = _series_exposure()
        assert exposure["table"] == _SERIES_TABLE
        assert exposure["schema"] == "public"
        assert list(exposure["columns"]) == _SERIES_COLUMNS
        assert exposure["order_by"] == "bucket ASC"
        assert exposure["freshness_column"] == "bucket"
        # A year of daily buckets is the sensible upper bound for the series.
        assert exposure["limit"] == 365

    def test_series_topic_in_externally_consumed(self) -> None:
        contract = yaml.safe_load(_CONTRACT_PATH.read_text())
        assert _SERIES_TOPIC in contract["externally_consumed_topics"]

    def test_series_exposure_passes_materialization_ratchet(self) -> None:
        # Proves the exposure carries a node-local migration that creates the
        # view (materialization authority + cold DDL proof). Without migration
        # 078 the projection API would mark the topic DEGRADED at startup.
        manifest = _ManifestStub((_ContractStub("projection_savings", _CONTRACT_PATH),))
        issues = validate_projection_materialization_contracts(manifest)
        series_issues = [
            issue for issue in issues if issue.table_or_view == _SERIES_TABLE
        ]
        assert series_issues == [], (
            "series exposure failed materialization ratchet: "
            + "; ".join(issue.format() for issue in series_issues)
        )
        # The whole contract must stay clean too.
        assert issues == ()
