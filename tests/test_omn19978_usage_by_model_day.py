# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19978: usage-by-model-day projection (tokens in, tokens out, cost per model per UTC day).

AC1 -- ten recorded calls over two UTC days and two models give four rows whose
token and cost sums equal the call rows'. Run through the REAL SQLite adapter,
so the store-neutral write path is the thing under test, not a double.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.enums.enum_usage_source import EnumUsageSource
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


def _sourced(
    call_id: str, cost: float, source: str | None, *, tenant: str = "omninode"
) -> ModelUsageCallEvent:
    payload: dict[str, object] = {
        "call_id": call_id,
        "model_name": "m",
        "timestamp": "2026-09-28T00:00:00Z",
        "prompt_tokens": 10,
        "completion_tokens": 2,
        "estimated_cost_usd": cost,
        "tenant_id": tenant,
    }
    if source is not None:
        payload["usage_source"] = source
    return ModelUsageCallEvent.model_validate(payload)


def _aggregate_after(
    tmp_path: Path, *events: ModelUsageCallEvent
) -> tuple[SqliteDatabaseAdapter, dict[str, object]]:
    db = SqliteDatabaseAdapter(tmp_path / "usage.sqlite")
    fold = HandlerProjectionUsageByModelDay()
    for event in events:
        apply_usage_call(fold.handle(event), db)
    (row,) = db.query(AGGREGATE_TABLE)
    return db, row


def test_usage_source_is_stored_and_estimated_cost_is_not_measured(
    tmp_path: Path,
) -> None:
    """AC3: one measured and one estimated call for the same day and model.

    The measured cost is the measured call's alone, the estimated call is counted
    as unmeasured, and ``cost_usd``/``call_count`` keep their meaning (every call).
    """
    db, row = _aggregate_after(
        tmp_path,
        _sourced("measured-1", 0.25, "measured"),
        _sourced("estimated-1", 9.0, "estimated"),
    )
    calls = {r["call_id"]: r["usage_source"] for r in db.query(CALLS_TABLE)}
    assert calls == {"measured-1": "measured", "estimated-1": "estimated"}
    assert Decimal(str(row["measured_cost_usd"])) == Decimal("0.25")
    assert int(str(row["unmeasured_call_count"])) == 1
    assert Decimal(str(row["cost_usd"])) == Decimal("9.25")
    assert int(str(row["call_count"])) == 2
    assert (int(str(row["input_tokens"])), int(str(row["output_tokens"]))) == (20, 4)


def test_unmeasured_only_aggregate_has_typed_null_cost(tmp_path: Path) -> None:
    """AC4: no measured call means no measured cost -- NULL, never 0."""
    _, row = _aggregate_after(tmp_path, _sourced("unknown-1", 4.0, "unknown"))
    assert row["measured_cost_usd"] is None
    assert int(str(row["unmeasured_call_count"])) == 1


def test_a_measured_call_costing_nothing_is_a_measured_zero_not_null(
    tmp_path: Path,
) -> None:
    _, row = _aggregate_after(tmp_path, _sourced("free-1", 0.0, "measured"))
    assert row["measured_cost_usd"] is not None
    assert Decimal(str(row["measured_cost_usd"])) == Decimal(0)
    assert int(str(row["unmeasured_call_count"])) == 0


def test_a_call_with_no_usage_source_is_unmeasured_not_assumed_measured(
    tmp_path: Path,
) -> None:
    """An event that never said how its cost was obtained is not a measurement."""
    event = _sourced("silent-1", 3.0, None)
    assert event.usage_source == EnumUsageSource.UNKNOWN
    _, row = _aggregate_after(tmp_path, event)
    assert row["measured_cost_usd"] is None
    assert int(str(row["unmeasured_call_count"])) == 1


def test_the_legacy_api_source_reads_as_measured() -> None:
    """llm_call_metrics stores the legacy alias API for a measured call."""
    assert _sourced("api-1", 1.0, "API").usage_source == EnumUsageSource.MEASURED


def test_the_fold_carries_the_usage_source_into_the_delta() -> None:
    delta = HandlerProjectionUsageByModelDay().handle(_sourced("e-1", 1.0, "estimated"))
    assert delta.usage_source == EnumUsageSource.ESTIMATED


def test_replaying_a_mixed_key_changes_nothing(tmp_path: Path) -> None:
    events = (
        _sourced("measured-1", 0.25, "measured"),
        _sourced("estimated-1", 9.0, "estimated"),
    )
    db, before = _aggregate_after(tmp_path, *events)
    fold = HandlerProjectionUsageByModelDay()
    for event in events:
        assert apply_usage_call(fold.handle(event), db) is False
    assert db.query(AGGREGATE_TABLE) == [before]


def test_an_old_local_store_gains_the_new_columns_on_connect(tmp_path: Path) -> None:
    """A store written before this change keeps working: old rows read unknown/NULL/0."""
    path = tmp_path / "old.sqlite"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE usage_by_model_day_calls (call_id TEXT PRIMARY KEY, "
        "tenant_id TEXT NOT NULL, usage_day TEXT NOT NULL, model_id TEXT NOT NULL, "
        "input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL, "
        "cost_usd REAL NOT NULL, occurred_at TEXT NOT NULL, ingested_at TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE usage_by_model_day (tenant_id TEXT NOT NULL, "
        "usage_day TEXT NOT NULL, model_id TEXT NOT NULL, "
        "input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL, "
        "cost_usd REAL NOT NULL, call_count INTEGER NOT NULL, "
        "updated_at TEXT NOT NULL, PRIMARY KEY (tenant_id, usage_day, model_id))"
    )
    conn.execute(
        "INSERT INTO usage_by_model_day_calls VALUES "
        "('old-1', 'omninode', '2026-09-27', 'm', 1, 1, 0.5, 'x', 'y')"
    )
    conn.execute(
        "INSERT INTO usage_by_model_day VALUES "
        "('omninode', '2026-09-27', 'm', 1, 1, 0.5, 1, 'y')"
    )
    conn.commit()
    conn.close()

    db = SqliteDatabaseAdapter(path)
    (old,) = db.query(AGGREGATE_TABLE)
    assert old["measured_cost_usd"] is None
    assert int(str(old["unmeasured_call_count"])) == 0
    assert db.query(CALLS_TABLE)[0]["usage_source"] == "unknown"
    # The next call for that key recounts it with the old call as unmeasured.
    apply_usage_call(
        HandlerProjectionUsageByModelDay().handle(
            ModelUsageCallEvent.model_validate(
                {
                    "call_id": "new-1",
                    "model_name": "m",
                    "timestamp": "2026-09-27T10:00:00Z",
                    "estimated_cost_usd": 0.1,
                    "usage_source": "measured",
                    "tenant_id": "omninode",
                }
            )
        ),
        db,
    )
    (row,) = db.query(AGGREGATE_TABLE)
    assert Decimal(str(row["measured_cost_usd"])) == Decimal("0.1")
    assert int(str(row["unmeasured_call_count"])) == 1
    assert int(str(row["call_count"])) == 2


def test_a_store_written_by_the_previous_release_opens_and_keeps_its_relabel(
    tmp_path: Path,
) -> None:
    """The store every existing install has: llm_call_metrics already carries
    usage_source (so the connect-time relabel UPDATE runs and opens an implicit
    transaction), and the usage tables predate the new columns and triggers.
    Opening it must add the columns, and must commit the relabel, not undo it."""
    path = tmp_path / "previous-release.sqlite"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE llm_call_metrics (correlation_id TEXT, usage_source TEXT, "
        "input_hash TEXT NOT NULL UNIQUE)"
    )
    conn.execute("INSERT INTO llm_call_metrics VALUES ('c-1', 'API', 'h-1')")
    conn.execute(
        "CREATE TABLE usage_by_model_day_calls (call_id TEXT PRIMARY KEY, "
        "tenant_id TEXT NOT NULL, usage_day TEXT NOT NULL, model_id TEXT NOT NULL, "
        "input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL, "
        "cost_usd REAL NOT NULL, occurred_at TEXT NOT NULL, ingested_at TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE usage_by_model_day (tenant_id TEXT NOT NULL, "
        "usage_day TEXT NOT NULL, model_id TEXT NOT NULL, "
        "input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL, "
        "cost_usd REAL NOT NULL, call_count INTEGER NOT NULL, "
        "updated_at TEXT NOT NULL, PRIMARY KEY (tenant_id, usage_day, model_id))"
    )
    conn.commit()
    conn.close()

    assert SqliteDatabaseAdapter(path).query(AGGREGATE_TABLE) == []

    check = sqlite3.connect(path)
    try:
        columns = {r[1] for r in check.execute("PRAGMA table_info(usage_by_model_day)")}
        relabelled = check.execute(
            "SELECT usage_source FROM llm_call_metrics WHERE input_hash = 'h-1'"
        ).fetchone()
    finally:
        check.close()
    assert {
        "measured_cost_usd",
        "unmeasured_call_count",
        "projection_cursor",
    } <= columns
    assert relabelled == ("measured",)


def test_each_insert_and_recount_takes_the_next_cursor(tmp_path: Path) -> None:
    """The served exposure walks by projection_cursor, so a local store must
    stamp one, and re-stamp a recounted row the way the Postgres BIGSERIAL does."""
    db = SqliteDatabaseAdapter(tmp_path / "usage.sqlite")
    fold = HandlerProjectionUsageByModelDay()
    apply_usage_call(fold.handle(_sourced("a", 1.0, "measured")), db)
    other = _sourced("b", 1.0, "measured").model_copy(update={"model_name": "n"})
    apply_usage_call(fold.handle(other), db)
    cursors = {r["model_id"]: r["projection_cursor"] for r in db.query(AGGREGATE_TABLE)}
    assert cursors == {"m": 1, "n": 2}
    apply_usage_call(fold.handle(_sourced("c", 1.0, "measured")), db)
    cursors = {r["model_id"]: r["projection_cursor"] for r in db.query(AGGREGATE_TABLE)}
    assert cursors == {"m": 3, "n": 2}


def test_the_measured_cost_migration_is_additive() -> None:
    """Existing rows read unknown / NULL / 0; nothing is dropped or rewritten."""
    sql = (
        NODE_DIR / "migrations" / "0002_usage_by_model_day_measured_cost.sql"
    ).read_text()
    for statement in (
        "ALTER TABLE public.usage_by_model_day_calls ADD COLUMN IF NOT EXISTS "
        "usage_source TEXT NOT NULL DEFAULT 'unknown'",
        "ALTER TABLE public.usage_by_model_day ADD COLUMN IF NOT EXISTS "
        "measured_cost_usd NUMERIC(18,8)",
        "ALTER TABLE public.usage_by_model_day ADD COLUMN IF NOT EXISTS "
        "unmeasured_call_count INTEGER NOT NULL DEFAULT 0",
    ):
        assert statement in sql
    for forbidden in ("DROP ", "UPDATE ", "DELETE ", "TRUNCATE "):
        assert forbidden not in sql.upper()


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
    for column in (
        "input_tokens",
        "output_tokens",
        "cost_usd",
        "measured_cost_usd",
        "unmeasured_call_count",
        "call_count",
    ):
        assert column in exposure.columns


def test_exposure_is_tenant_scoped() -> None:
    """AC-T: a read for one tenant never carries another tenant's rows."""
    contract = _contract()
    exposures = load_projection_exposures_from_contract(
        contract, str(contract["name"]), CONTRACT_PATH
    )
    assert len(exposures) == 1
    exposure = exposures[0]
    assert exposure.tenant_column == "tenant_id"


def test_the_reader_opt_out_is_gone_now_that_the_usage_page_reads_it() -> None:
    # OMN-17199: `consumers: none` standing over a live reader fails the
    # omnibase_infra exposure-reader-coverage gate (stale_opt_out), and any other
    # `consumers` value fails closed. The Usage page (omnidash, OMN-20006) is the
    # declared reader, so both keys are deleted, not rewritten.
    api = _contract()["projection_api"]
    assert isinstance(api, dict)
    assert api["bus_backed"] is True
    assert "consumers" not in api
    assert "consumers_reason" not in api


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
