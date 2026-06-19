# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-13350 — generation_events carries the validator-acceptance (corpus) verdict.

The omnimarket 0.4.3 generation handler emits ModelGenerationBenchmark, which
since OMN-13289 carries corpus_checked / corpus_passed / corpus_errors on
onex.evt.omnimarket.node-generation-completed.v1. generation_events had no such
columns, so the projection INSERT raised UndefinedColumn and EVERY completion
event was dropped (the consumer committed the offset anyway → silent drop).

Tests (written from the acceptance criteria):
1. Migration 0015 declares corpus_checked / corpus_passed / corpus_errors,
   idempotent, citing the ticket.
2. The contract points generation_events at the 0015 migration and exposes the
   three corpus columns on the projection_api exposure.
3. The sync live-runtime write path (HandlerProjectionDelegation) populates all
   three corpus fields on the upserted row — no UndefinedColumn, no drop.
4. The async runner write path emits the three corpus columns in its INSERT and
   passes the corpus_errors as a JSON-serialized ::jsonb param.
5. The emitted benchmark model carries the corpus fields (producer side).
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

_NODE_DIR = (
    Path(__file__).parent.parent / "src/omnimarket/nodes/node_projection_delegation"
)
MIGRATION_PATH = _NODE_DIR / "migrations/0015_generation_corpus_acceptance.sql"
CONTRACT_PATH = _NODE_DIR / "contract.yaml"
ASYNC_HANDLER_PATH = _NODE_DIR / "handlers/handler_delegation.py"

CORPUS_COLUMNS = ("corpus_checked", "corpus_passed", "corpus_errors")
GENERATION_EXPOSURE_TOPIC = "onex.evt.omnimarket.node-generation-completed.v1"


# ---------------------------------------------------------------------------
# 1. Migration declares the corpus columns
# ---------------------------------------------------------------------------


def test_migration_file_exists() -> None:
    assert MIGRATION_PATH.exists(), f"Migration not found: {MIGRATION_PATH}"


def test_migration_declares_corpus_columns() -> None:
    sql = MIGRATION_PATH.read_text()
    for column in CORPUS_COLUMNS:
        assert column in sql, f"Migration must add {column} column"
    assert "JSONB" in sql, "corpus_errors must be a JSONB column"


def test_migration_is_idempotent_and_cites_ticket() -> None:
    sql = MIGRATION_PATH.read_text()
    assert "IF NOT EXISTS" in sql
    assert "OMN-13350" in sql


# ---------------------------------------------------------------------------
# 2. Contract points generation_events at the migration and exposes corpus
# ---------------------------------------------------------------------------


def test_contract_references_corpus_migration() -> None:
    contract = yaml.safe_load(CONTRACT_PATH.read_text())
    tables = contract["db_io"]["db_tables"]
    gen = next(t for t in tables if t["name"] == "generation_events")
    assert gen["migration"] == "0015_generation_corpus_acceptance.sql"


def test_contract_exposes_corpus_columns() -> None:
    contract = yaml.safe_load(CONTRACT_PATH.read_text())
    exposures = contract["projection_api"]["exposures"]
    gen = next(
        e
        for e in exposures
        if e["table"] == "generation_events" and e["topic"] == GENERATION_EXPOSURE_TOPIC
    )
    for column in CORPUS_COLUMNS:
        assert column in gen["columns"], f"exposure must surface {column}"


# ---------------------------------------------------------------------------
# 3. Sync live-runtime write path populates the corpus fields (no drop)
# ---------------------------------------------------------------------------


def _project_generation(payload_extra: dict[str, object]) -> dict[str, object]:
    from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
        HandlerProjectionDelegation,
    )

    captured: list[dict[str, object]] = []

    class _RecordingDB:
        def upsert(self, table: str, conflict_key: str, row: dict[str, object]) -> bool:
            captured.append(row)
            return True

        def query(
            self, table: str, filters: dict[str, object] | None = None
        ) -> list[dict[str, object]]:
            return []

    payload: dict[str, object] = {
        "_db": _RecordingDB(),
        "_event_type": "onex.evt.omnimarket.node-generation-completed.v1",
        "correlation_id": "gen-corpus-001",
        "task_description": "generate a validator",
        "provider": "local",
        "model_id": "qwen3-coder",
        "endpoint_class": "local-coder",
        "attempt_count": 1,
        "total_latency_e2e_ms": 1234,
        "contract_passed": True,
        "cost_inference_usd": 0.0,
        "contract_yaml": "name: node_x\n",
        "handler_source": "def handle(input_data):\n    return {}\n",
        "routing_source": "contract",
        "resolved_endpoint": "http://host:8000/v1/chat/completions",
    }
    payload.update(payload_extra)
    HandlerProjectionDelegation().handle(payload)
    assert captured, "expected one generation_events upsert"
    return captured[0]


def test_sync_path_records_corpus_failure() -> None:
    """A validator-generation run that did not pass the corpus."""
    row = _project_generation(
        {
            "corpus_checked": True,
            "corpus_passed": False,
            "corpus_errors": ["missed violation_fixture v3", "false-flagged clean c1"],
        }
    )
    assert row["corpus_checked"] is True
    assert row["corpus_passed"] is False
    assert row["corpus_errors"] == [
        "missed violation_fixture v3",
        "false-flagged clean c1",
    ]


def test_sync_path_records_corpus_success() -> None:
    row = _project_generation({"corpus_checked": True, "corpus_passed": True})
    assert row["corpus_checked"] is True
    assert row["corpus_passed"] is True
    assert row["corpus_errors"] == []


def test_sync_path_defaults_corpus_fields_when_absent() -> None:
    """Ordinary free-text generation (no corpus gate) defaults to not-checked."""
    row = _project_generation({})
    assert row["corpus_checked"] is False
    assert row["corpus_passed"] is False
    assert row["corpus_errors"] == []


def test_sync_path_full_payload_projects_without_undefined_column() -> None:
    """The regression: the full 0.4.3 completion payload (semantic_* AND corpus_*)
    must project through the canonical adapter shape without raising. The
    inmemory adapter mirrors the column-name contract the live psycopg2 adapter
    uses, so a row with every emitted field upserts cleanly."""
    from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
        HandlerProjectionDelegation,
    )
    from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

    db = InmemoryDatabaseAdapter()
    payload: dict[str, object] = {
        "_db": db,
        "_event_type": "onex.evt.omnimarket.node-generation-completed.v1",
        "correlation_id": "gen-corpus-full-001",
        "task_description": "generate a validator",
        "provider": "local",
        "model_id": "qwen3-coder",
        "endpoint_class": "local-coder",
        "attempt_count": 2,
        "total_latency_e2e_ms": 4321,
        "contract_passed": True,
        "semantic_checked": True,
        "semantic_passed": False,
        "corpus_checked": True,
        "corpus_passed": False,
        "corpus_errors": ["missed v3"],
        "cost_inference_usd": 0.0,
        "contract_yaml": "name: node_x\n",
        "handler_source": "def handle(input_data):\n    return {}\n",
        "routing_source": "contract",
        "resolved_endpoint": "http://host:8000/v1/chat/completions",
    }
    HandlerProjectionDelegation().handle(payload)
    rows = db.query("generation_events", {"correlation_id": "gen-corpus-full-001"})
    assert len(rows) == 1
    row = rows[0]
    assert row["semantic_checked"] is True
    assert row["corpus_checked"] is True
    assert row["corpus_passed"] is False
    assert row["corpus_errors"] == ["missed v3"]


# ---------------------------------------------------------------------------
# 4. Async runner write path emits the corpus columns
# ---------------------------------------------------------------------------


def test_async_insert_includes_corpus_columns() -> None:
    source = ASYNC_HANDLER_PATH.read_text()
    for column in CORPUS_COLUMNS:
        assert column in source, (
            f"async _project_generation_completed INSERT must include {column}"
        )
    assert "::jsonb" in source, "corpus_errors must be cast to jsonb in the INSERT"


def test_async_runner_passes_corpus_params_to_db() -> None:
    import asyncio
    from unittest.mock import AsyncMock

    from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation import (
        DelegationProjectionRunner,
    )
    from omnimarket.projection.runner import MessageMeta

    captured: list[tuple[tuple[object, ...], dict[str, object]]] = []

    class _RecordingDB:
        async def execute(self, *args: object, **kwargs: object) -> None:
            captured.append((args, kwargs))

    runner = DelegationProjectionRunner()
    runner._db = _RecordingDB()  # type: ignore[assignment]
    runner._publish_fn = AsyncMock(return_value=None)  # type: ignore[assignment]

    topic = runner._topic_generation
    data: dict[str, object] = {
        "correlation_id": "gen-async-corpus-001",
        "task_description": "generate a validator",
        "contract_yaml": "name: node_x\n",
        "handler_source": "def handle(input_data):\n    return {}\n",
        "contract_passed": True,
        "corpus_checked": True,
        "corpus_passed": False,
        "corpus_errors": ["missed v3", "false-flagged c1"],
        "attempt_count": 1,
        "total_latency_e2e_ms": 100,
        "routing_source": "contract",
        "resolved_endpoint": "http://host:8000/v1/chat/completions",
    }
    meta = MessageMeta(partition=0, offset=0, fallback_id="gen-async-corpus-001")
    ok = asyncio.run(runner.project_event(topic, data, meta))
    assert ok is True
    assert captured, "expected one DB write"
    sql = str(captured[0][0][0])
    params = list(captured[0][0][1:])
    for column in CORPUS_COLUMNS:
        assert column in sql
    # corpus_checked / corpus_passed booleans + corpus_errors JSON string present.
    assert True in params
    assert False in params
    assert json.dumps(["missed v3", "false-flagged c1"]) in params


# ---------------------------------------------------------------------------
# 5. The emitted benchmark model carries the corpus fields (producer side)
# ---------------------------------------------------------------------------


def test_benchmark_model_carries_corpus_fields() -> None:
    from omnimarket.nodes.node_generation_consumer.models.model_generation import (
        ModelGenerationBenchmark,
    )

    benchmark = ModelGenerationBenchmark(
        correlation_id="c1",
        task_description="t",
        corpus_checked=True,
        corpus_passed=False,
        corpus_errors=["missed v3"],
    )
    dumped = benchmark.model_dump()
    assert dumped["corpus_checked"] is True
    assert dumped["corpus_passed"] is False
    assert dumped["corpus_errors"] == ["missed v3"]


def test_projection_event_model_has_corpus_fields() -> None:
    from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
        ModelProjectionGenerationCompletedEvent,
    )

    event = ModelProjectionGenerationCompletedEvent(
        correlation_id="c1",
        corpus_checked=True,
        corpus_passed=False,
        corpus_errors=["missed v3"],
    )
    assert event.corpus_checked is True
    assert event.corpus_passed is False
    assert event.corpus_errors == ["missed v3"]
