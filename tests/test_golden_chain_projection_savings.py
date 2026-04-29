"""Golden chain tests for node_projection_savings."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

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
        }

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
            contract["event_bus"]["consumer_group"]
            == "local.omnibase_infra.node_projection_savings.consume.v1"
        )

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
    stable = {key: str(value) for key, value in row.items() if key != "created_at"}
    encoded = json.dumps(stable, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
