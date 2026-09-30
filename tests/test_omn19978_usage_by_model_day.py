# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19978: usage-by-model-day projection (tokens in, tokens out, cost per model per UTC day).

AC1 -- ten recorded calls over two UTC days and two models give four rows whose
token and cost sums equal the call rows'. Run through the REAL SQLite adapter,
so the store-neutral write path is the thing under test, not a double.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_projection_usage_by_model_day.handlers.handler_projection_usage_by_model_day import (
    HandlerProjectionUsageByModelDay,
)
from omnimarket.nodes.node_projection_usage_by_model_day.handlers.handler_usage_by_model_day_store import (
    AGGREGATE_TABLE,
    CALLS_TABLE,
    apply_usage_call,
)
from omnimarket.nodes.node_projection_usage_by_model_day.handlers.handler_usage_by_model_day_writer import (
    UsageByModelDayProjectionWriter,
)
from omnimarket.nodes.node_projection_usage_by_model_day.models.model_usage_call_event import (
    ModelUsageCallEvent,
)
from omnimarket.projection.discovery import load_projection_exposures_from_contract
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter

pytestmark = pytest.mark.unit

NODE_DIR = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_usage_by_model_day"
)
CONTRACT_PATH = NODE_DIR / "contract.yaml"

# (call_id, model, timestamp, prompt_tokens, completion_tokens, cost_usd)
CALLS: list[tuple[str, str, str, int, int, str]] = [
    ("c01", "qwen3-coder", "2026-09-28T01:00:00Z", 100, 10, "0.001"),
    ("c02", "qwen3-coder", "2026-09-28T12:00:00Z", 200, 20, "0.002"),
    ("c03", "qwen3-coder", "2026-09-28T23:59:59Z", 300, 30, "0.003"),
    ("c04", "glm-4.6", "2026-09-28T02:00:00Z", 400, 40, "0.010"),
    ("c05", "glm-4.6", "2026-09-28T03:00:00Z", 500, 50, "0.020"),
    ("c06", "qwen3-coder", "2026-09-29T00:00:00Z", 600, 60, "0.004"),
    ("c07", "qwen3-coder", "2026-09-29T10:00:00Z", 700, 70, "0.005"),
    ("c08", "glm-4.6", "2026-09-29T11:00:00Z", 800, 80, "0.030"),
    ("c09", "glm-4.6", "2026-09-29T12:00:00Z", 900, 90, "0.040"),
    ("c10", "glm-4.6", "2026-09-29T13:00:00Z", 1000, 100, "0.050"),
]


def _event(call: tuple[str, str, str, int, int, str]) -> ModelUsageCallEvent:
    call_id, model, ts, prompt, completion, cost = call
    return ModelUsageCallEvent.model_validate(
        {
            "call_id": call_id,
            "model_name": model,
            "timestamp": ts,
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "estimated_cost_usd": float(cost),
            "tenant_id": "omninode",
        }
    )


def _apply_all(db: SqliteDatabaseAdapter) -> None:
    fold = HandlerProjectionUsageByModelDay()
    for call in CALLS:
        apply_usage_call(fold.handle(_event(call)), db)


def test_ten_calls_give_four_rows_whose_sums_equal_the_call_rows(
    tmp_path: Path,
) -> None:
    db = SqliteDatabaseAdapter(tmp_path / "usage.sqlite")
    _apply_all(db)
    rows = db.query(AGGREGATE_TABLE)
    assert len(rows) == 4
    assert sum(int(r["input_tokens"]) for r in rows) == sum(c[3] for c in CALLS)
    assert sum(int(r["output_tokens"]) for r in rows) == sum(c[4] for c in CALLS)
    assert sum(Decimal(str(r["cost_usd"])) for r in rows) == sum(
        Decimal(c[5]) for c in CALLS
    )
    assert sum(int(r["call_count"]) for r in rows) == 10
    by_key = {(r["usage_day"], r["model_id"]): r for r in rows}
    glm_29 = by_key[("2026-09-29", "glm-4.6")]
    assert int(glm_29["input_tokens"]) == 800 + 900 + 1000
    assert int(glm_29["output_tokens"]) == 80 + 90 + 100
    assert Decimal(str(glm_29["cost_usd"])) == Decimal("0.120")
    assert int(glm_29["call_count"]) == 3


def test_replaying_every_call_changes_nothing(tmp_path: Path) -> None:
    db = SqliteDatabaseAdapter(tmp_path / "usage.sqlite")
    _apply_all(db)
    before = sorted(
        db.query(AGGREGATE_TABLE),
        key=lambda r: str(r["model_id"]) + str(r["usage_day"]),
    )
    _apply_all(db)
    after = sorted(
        db.query(AGGREGATE_TABLE),
        key=lambda r: str(r["model_id"]) + str(r["usage_day"]),
    )
    assert before == after
    assert len(db.query(CALLS_TABLE)) == 10


def test_the_day_is_the_utc_day_not_the_senders_local_day() -> None:
    event = ModelUsageCallEvent.model_validate(
        {
            "call_id": "tz1",
            "model_name": "m",
            "timestamp": "2026-09-28T23:30:00-04:00",
            "prompt_tokens": 1,
            "completion_tokens": 1,
        }
    )
    delta = HandlerProjectionUsageByModelDay().handle(event)
    assert delta.usage_day == "2026-09-29"


def test_a_call_with_no_usable_timestamp_is_refused_not_dated_by_the_clock() -> None:
    with pytest.raises(ValidationError):
        ModelUsageCallEvent.model_validate(
            {"call_id": "x", "model_name": "m", "prompt_tokens": 1}
        )


def test_fold_is_pure_and_replay_deterministic() -> None:
    fold = HandlerProjectionUsageByModelDay()
    event = _event(CALLS[0])
    assert fold.handle(event) == fold.handle(event)


def test_a_missing_tenant_falls_to_the_house_tenant_not_an_empty_key() -> None:
    event = ModelUsageCallEvent.model_validate(
        {
            "call_id": "t1",
            "model_name": "m",
            "timestamp": "2026-09-28T00:00:00Z",
            "prompt_tokens": 1,
            "completion_tokens": 1,
        }
    )
    assert HandlerProjectionUsageByModelDay().handle(event).tenant_id == "omninode"


def test_no_postgres_only_sql_is_reachable_from_the_store_neutral_path() -> None:
    source = (NODE_DIR / "handlers" / "handler_usage_by_model_day_store.py").read_text()
    for token in ("$1", "::jsonb", "::usage_source_type", "NOW()"):
        assert token not in source


def _contract() -> dict[str, object]:
    loaded = yaml.safe_load(CONTRACT_PATH.read_text())
    assert isinstance(loaded, dict)
    return loaded


def test_exposure_is_bus_backed_at_the_model_day_grain() -> None:
    contract = _contract()
    exposures = load_projection_exposures_from_contract(
        contract, str(contract["name"]), CONTRACT_PATH
    )
    assert len(exposures) == 1
    exposure = exposures[0]
    assert exposure.topic == "onex.snapshot.projection.usage-by-model-day.v1"
    assert exposure.bus_backed is True
    assert exposure.cursor_column == "projection_cursor"
    assert exposure.key_columns == ("tenant_id", "usage_day", "model_id")
    for column in ("input_tokens", "output_tokens", "cost_usd", "call_count"):
        assert column in exposure.columns


def test_runtime_dispatch_resolves_only_the_writer() -> None:
    contract = _contract()
    handlers = contract["handler_routing"]["handlers"]  # type: ignore[index]
    assert [h["handler"]["name"] for h in handlers] == [
        "UsageByModelDayProjectionWriter"
    ]
    assert contract["event_bus"]["subscribe_topics"] == [  # type: ignore[index]
        "onex.evt.omniintelligence.llm-call-completed.v1"
    ]


def test_the_writer_declares_in_process_dispatch() -> None:
    assert UsageByModelDayProjectionWriter.onex_runtime_inprocess_dispatch is True


def test_the_postgres_migration_declares_both_tables() -> None:
    sql = (NODE_DIR / "migrations" / "0000_create_usage_by_model_day.sql").read_text()
    assert "usage_by_model_day_calls" in sql
    assert "usage_by_model_day" in sql
    assert "PRIMARY KEY (tenant_id, usage_day, model_id)" in sql
    assert "projection_cursor" in sql
