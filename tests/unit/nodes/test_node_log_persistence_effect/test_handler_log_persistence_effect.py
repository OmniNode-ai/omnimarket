# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Unit tests for NodeLogPersistenceEffect.

No real database — asyncpg pool is mocked throughout.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
import yaml

from omnimarket.nodes.node_log_persistence_effect.handlers.handler_log_persistence_effect import (
    ModelLogPersistenceResult,
    NodeLogPersistenceEffect,
)
from omnimarket.nodes.node_log_projection.handlers.handler_log_projection import (
    EnumLogLevel,
    ModelLogEntry,
)


def _make_entry(**kwargs: Any) -> ModelLogEntry:
    defaults: dict[str, Any] = {
        "node_name": "test_node",
        "message": "hello world",
    }
    defaults.update(kwargs)
    return ModelLogEntry(**defaults)


def _make_pool(fetchval_return: Any = "some-entry-id") -> MagicMock:
    """Build a mock asyncpg pool whose conn.fetchval returns fetchval_return."""
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=fetchval_return)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AsyncContextManager(conn))
    return pool, conn


class _AsyncContextManager:
    """Minimal async context manager wrapping a value."""

    def __init__(self, value: Any) -> None:
        self._value = value

    async def __aenter__(self) -> Any:
        return self._value

    async def __aexit__(self, *args: Any) -> None:
        pass


# ---------------------------------------------------------------------------
# Deserialization
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_model_log_entry_round_trip() -> None:
    entry = _make_entry(
        node_name="my_node",
        function_name="do_thing",
        level=EnumLogLevel.ERROR,
        message="boom",
        correlation_id="corr-123",
        duration_ms=42.5,
        metadata={"key": "val"},
    )
    assert entry.node_name == "my_node"
    assert entry.level == EnumLogLevel.ERROR
    assert entry.correlation_id == "corr-123"
    assert entry.metadata == {"key": "val"}


# ---------------------------------------------------------------------------
# Graceful degradation — no pool
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_skips_when_pool_is_none() -> None:
    handler = NodeLogPersistenceEffect(pool=None, pg_dsn="")
    entry = _make_entry()

    result = await handler.persist(entry)

    assert isinstance(result, ModelLogPersistenceResult)
    assert result.status == "skipped"
    assert result.entry_id == entry.entry_id


# ---------------------------------------------------------------------------
# Successful insert
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_inserts_correct_params() -> None:
    entry = _make_entry(
        node_name="my_node",
        function_name="fn",
        level=EnumLogLevel.WARNING,
        message="watch out",
        correlation_id="c-1",
        duration_ms=10.0,
        metadata={"env": "test"},
    )
    pool, conn = _make_pool(fetchval_return=entry.entry_id)
    handler = NodeLogPersistenceEffect(pool=pool)

    result = await handler.persist(entry)

    assert result.status == "written"
    assert result.entry_id == entry.entry_id

    conn.fetchval.assert_awaited_once()
    call_args = conn.fetchval.call_args
    positional = call_args.args

    assert positional[1] == entry.entry_id
    assert positional[2] == entry.timestamp
    assert positional[3] == entry.node_name
    assert positional[4] == entry.function_name
    assert positional[5] == entry.level.value
    assert positional[6] == entry.message
    assert positional[7] == entry.correlation_id
    assert positional[8] == entry.duration_ms
    assert json.loads(positional[9]) == entry.metadata


# ---------------------------------------------------------------------------
# Idempotent insert (conflict — row already exists)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_idempotent_when_conflict() -> None:
    entry = _make_entry()
    pool, _conn = _make_pool(fetchval_return=None)
    handler = NodeLogPersistenceEffect(pool=pool)

    result = await handler.persist(entry)

    assert result.status == "idempotent"
    assert result.entry_id == entry.entry_id


# ---------------------------------------------------------------------------
# DB error — returns error status, does not raise
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_returns_error_on_db_exception() -> None:
    entry = _make_entry()
    conn = AsyncMock()
    conn.fetchval = AsyncMock(side_effect=RuntimeError("DB is down"))
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AsyncContextManager(conn))
    handler = NodeLogPersistenceEffect(pool=pool)

    result = await handler.persist(entry)

    assert result.status == "error"
    assert result.error_message is not None
    assert "DB is down" in result.error_message


# ---------------------------------------------------------------------------
# handle_raw synchronous shim
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_handle_raw_returns_skipped_status() -> None:
    entry = _make_entry()
    raw = entry.model_dump(mode="json")
    result = NodeLogPersistenceEffect.handle_raw(raw)

    assert result["entry_id"] == entry.entry_id
    assert result["status"] == "skipped"


# ---------------------------------------------------------------------------
# Contract: terminal event
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_contract_declares_the_log_persistence_completed_terminal_event() -> None:
    """Runtime wiring normalizes the handler's typed return into an output
    event and publishes it through this contract-declared terminal topic
    (the handler itself never publishes — see the module docstring's
    handler-no-publish-access constraint). Pins the exact string so contract
    drift is caught here, not only at deploy time."""
    contract_path = (
        Path(__file__).parents[4]
        / "src"
        / "omnimarket"
        / "nodes"
        / "node_log_persistence_effect"
        / "contract.yaml"
    )
    contract = yaml.safe_load(contract_path.read_text())

    assert (
        contract["terminal_event"] == "onex.evt.omnimarket.log-persistence-completed.v1"
    )
    assert contract["terminal_event"] in contract["event_bus"]["publish_topics"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_canonical_logger_payload_reaches_runtime_database() -> None:
    """Exercise the real producer's wire model through the bus dispatch shape."""
    from omnibase_core.models.logging.model_structured_log_entry import (
        ModelStructuredLogEntry,
    )

    from omnimarket.logging.structured_logger import StructuredEventLogger
    from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

    correlation = uuid4()
    logger = StructuredEventLogger("node_build_loop")
    entry = await logger.error(
        "build failed", operation="build", correlation_id=correlation, duration_ms=12.5
    )
    payload = json.loads(entry.model_dump_json())
    # Exactly the model used by the actual producer, rather than a legacy fixture.
    ModelStructuredLogEntry.model_validate(payload)
    db = InmemoryDatabaseAdapter()
    payload.update(
        {
            "_db": db,
            "_topic": "onex.evt.platform.log-entry.v1",
            "_envelope_id": str(uuid4()),
        }
    )
    handler = NodeLogPersistenceEffect(pg_dsn="")
    result = handler.handle(payload)
    assert result["rows_upserted"] == 1
    rows = db.query("log_entries")
    assert len(rows) == 1
    assert rows[0]["entry_id"] == entry.entry_id
    assert rows[0]["timestamp"] == entry.timestamp
    assert rows[0]["node_name"] == "node_build_loop"
    assert rows[0]["function_name"] == "build"
    assert rows[0]["level"] == "error"
    assert rows[0]["correlation_id"] == str(correlation)
    assert rows[0]["duration_ms"] == 12.5
    handler.handle(payload)
    assert len(db.query("log_entries")) == 1


@pytest.mark.unit
@pytest.mark.parametrize("status", ["completed", "failed"])
def test_delegation_terminal_materializes_execution_log(status: str) -> None:
    from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

    db = InmemoryDatabaseAdapter()
    correlation, event_id = uuid4(), uuid4()
    timestamp = datetime(2026, 10, 6, tzinfo=UTC)
    payload = {
        "status": status,
        "correlation_id": str(correlation),
        "task_type": "document",
        "response": "private model output must not be copied into execution logs",
        "model_name": "lab-model",
        "quality_gate_passed": status == "completed",
        "execution_duration_ms": 125,
        "_db": db,
        "_topic": f"onex.evt.omnimarket.delegate-skill-{status}.v1",
        "_envelope_id": str(event_id),
        "_envelope_timestamp": timestamp,
    }
    handler = NodeLogPersistenceEffect(pg_dsn="")
    result = handler.handle(payload)
    assert result["rows_upserted"] == 1
    row = db.query("log_entries")[0]
    assert row["entry_id"] == event_id
    assert row["timestamp"] == timestamp
    assert row["correlation_id"] == str(correlation)
    assert row["message"] == f"Delegation {status}"
    assert row["level"] == ("info" if status == "completed" else "error")
    assert row["duration_ms"] == 125
    assert "private model output" not in str(row)
    handler.handle(payload)
    assert len(db.query("log_entries")) == 1
    # A different source event sharing a correlation remains distinct.
    payload["_envelope_id"] = str(uuid4())
    handler.handle(payload)
    assert len(db.query("log_entries")) == 2


@pytest.mark.unit
def test_runtime_dispatch_requires_database() -> None:
    with pytest.raises(TypeError, match="DatabaseAdapter"):
        NodeLogPersistenceEffect(pg_dsn="").handle({})


@pytest.mark.unit
def test_runtime_dispatch_rejects_non_mapping_request() -> None:
    with pytest.raises(TypeError, match="payload mapping"):
        NodeLogPersistenceEffect(pg_dsn="").handle("not-a-mapping")


@pytest.mark.unit
def test_runtime_dispatch_rejects_unknown_topic() -> None:
    from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

    db = InmemoryDatabaseAdapter()
    with pytest.raises(ValueError, match="unknown-topic"):
        NodeLogPersistenceEffect(pg_dsn="").handle(
            {"_db": db, "_topic": "unknown-topic"}
        )
    assert db.query("log_entries") == []


@pytest.mark.unit
def test_runtime_dispatch_rejects_missing_topic() -> None:
    from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

    db = InmemoryDatabaseAdapter()
    with pytest.raises(ValueError, match=r"topic.*None"):
        NodeLogPersistenceEffect(pg_dsn="").handle({"_db": db})
    assert db.query("log_entries") == []


@pytest.mark.unit
@pytest.mark.parametrize("status", ["completed", "failed"])
def test_delegation_terminal_requires_envelope_id(status: str) -> None:
    from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

    db = InmemoryDatabaseAdapter()
    payload = {
        "status": status,
        "correlation_id": str(uuid4()),
        "task_type": "document",
        "quality_gate_passed": status == "completed",
        "_db": db,
        "_topic": f"onex.evt.omnimarket.delegate-skill-{status}.v1",
        "_envelope_timestamp": datetime(2026, 10, 6, tzinfo=UTC),
    }
    with pytest.raises(ValueError, match="envelope id"):
        NodeLogPersistenceEffect(pg_dsn="").handle(payload)
    assert db.query("log_entries") == []


@pytest.mark.unit
@pytest.mark.parametrize("status", ["completed", "failed"])
def test_delegation_terminal_requires_envelope_timestamp(status: str) -> None:
    from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

    db = InmemoryDatabaseAdapter()
    payload = {
        "status": status,
        "correlation_id": str(uuid4()),
        "task_type": "document",
        "quality_gate_passed": status == "completed",
        "_db": db,
        "_topic": f"onex.evt.omnimarket.delegate-skill-{status}.v1",
        "_envelope_id": str(uuid4()),
    }
    with pytest.raises(ValueError, match="envelope timestamp"):
        NodeLogPersistenceEffect(pg_dsn="").handle(payload)
    assert db.query("log_entries") == []


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("duration_ms", ["not-a-number", ""])
async def test_structured_log_rejects_non_numeric_duration(duration_ms: str) -> None:
    from omnimarket.logging.structured_logger import StructuredEventLogger
    from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

    entry = await StructuredEventLogger("test_node").info("test log")
    payload = entry.model_dump(mode="json")
    payload["metadata"]["duration_ms"] = duration_ms
    db = InmemoryDatabaseAdapter()
    payload.update({"_db": db, "_topic": "onex.evt.platform.log-entry.v1"})
    with pytest.raises(ValueError, match="duration_ms must be convertible to float"):
        NodeLogPersistenceEffect(pg_dsn="").handle(payload)
    assert db.query("log_entries") == []


@pytest.mark.unit
def test_contract_event_models_resolve_to_supported_classes() -> None:
    from omnibase_core.models.logging.model_structured_log_entry import (
        ModelStructuredLogEntry,
    )

    from omnimarket.models.delegation.wire.model_delegate_skill_response import (
        ModelDelegateSkillCompleted,
        ModelDelegateSkillFailed,
    )
    from omnimarket.nodes.node_log_persistence_effect.handlers import (
        handler_log_persistence_effect as handler_module,
    )

    supported = {
        f"{cls.__module__}.{cls.__qualname__}": cls
        for cls in (
            ModelStructuredLogEntry,
            ModelDelegateSkillCompleted,
            ModelDelegateSkillFailed,
        )
    }
    contract_path = (
        Path(__file__).parents[4]
        / "src/omnimarket/nodes/node_log_persistence_effect/contract.yaml"
    )
    contract = yaml.safe_load(contract_path.read_text())
    assert supported == handler_module._SUPPORTED_EVENT_MODELS
    assert {
        route["topic"]: route["event_model"]
        for route in contract["handler_routing"]["handlers"]
    } == handler_module._HANDLER_EVENT_MODELS
    for route in contract["handler_routing"]["handlers"]:
        assert (
            handler_module._SUPPORTED_EVENT_MODELS[route["event_model"]]
            is supported[route["event_model"]]
        )


@pytest.mark.unit
def test_runtime_contract_routes_each_subscribed_topic() -> None:
    contract_path = (
        Path(__file__).parents[4]
        / "src/omnimarket/nodes/node_log_persistence_effect/contract.yaml"
    )
    contract = yaml.safe_load(contract_path.read_text())
    assert contract["handler_routing"]["routing_strategy"] == "topic_match"
    assert {h["topic"] for h in contract["handler_routing"]["handlers"]} == set(
        contract["event_bus"]["subscribe_topics"]
    )


@pytest.mark.unit
def test_runtime_selects_contract_database_dispatch_arm() -> None:
    """Guard the real runtime seam that previously bypassed database binding."""
    from omnibase_infra.runtime.auto_wiring.handler_wiring import (
        _typed_def_b_input_model,
    )

    assert _typed_def_b_input_model(NodeLogPersistenceEffect(pg_dsn="")) is None
