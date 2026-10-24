# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Concurrent session-content consumption keeps independent rows (OMN-21091)."""

from __future__ import annotations

import asyncio
import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter
from omnimarket.nodes.node_projection_session_content.handlers.handler_session_content import (
    SessionContentProjectionWriter,
)

NODE_DIR = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_projection_session_content"
)
TOPIC = "onex.cmd.omniintelligence.content-captured.v1"


def _wire(**overrides: Any) -> dict[str, Any]:
    """A record as the fan-out publishes it, after redact_capture ran."""
    record: dict[str, Any] = {
        "session_id": "11111111-2222-3333-4444-555555555555",
        "turn_id": "11111111-2222-3333-4444-555555555555:turn-4",
        "correlation_id": "11111111-2222-3333-4444-555555555555",
        "causation_id": None,
        "emitted_at": "2026-09-25T11:40:00.123456+00:00",
        "entity_id": "e-1",
        "schema_version": "1.0.0",
        "hook_source": "post_tool_use",
        "actor": "claude",
        "content_kind": "tool_response",
        "tool_name": "Bash",
        "tool_use_id": "toolu_01ABC",
        "chunk_index": 0,
        "chunk_count": 1,
        "content_sha256": "a" * 64,
        "original_chars": 11,
        "truncated": False,
        "producer_redaction": {"github_token": 1},
        "content": "hello [REDACTED:github_token] world",
        "command": "gh auth status",
        "redaction_state": "secret_detected",
        "lane": "full-content-capture-83",
    }
    record.update(overrides)
    return record


def _records(count: int) -> list[dict[str, Any]]:
    records = []
    for index in range(count):
        content = f"content record {index}"
        records.append(
            _wire(
                content=content,
                content_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
                original_chars=len(content),
            )
        )
    return records


class _RecordingDb:
    """Record exactly the statements and bindings sent to the adapter."""

    def __init__(self) -> None:
        self.statements: list[tuple[str, tuple[Any, ...]]] = []

    async def connect(self) -> None:
        """No pool to open."""

    async def close(self) -> None:
        """No pool to close."""

    async def execute(self, statement: str, *args: Any) -> str:
        self.statements.append((statement, args))
        return "INSERT 0 1"


class _BarrierDb(_RecordingDb):
    """A serial writer times out before four writes can reach the barrier."""

    def __init__(self) -> None:
        super().__init__()
        self.barrier = threading.Barrier(4, timeout=5)

    async def execute(self, statement: str, *args: Any) -> str:
        await asyncio.to_thread(self.barrier.wait)
        return await super().execute(statement, *args)


@pytest.mark.unit
def test_the_writer_declares_four_records_in_flight() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))
    assert contract["consume_concurrency"] == {"max_in_flight_records": 4}


@pytest.mark.unit
def test_each_runtime_message_gets_its_own_single_connection_adapter() -> None:
    writer = SessionContentProjectionWriter()
    bound = AsyncpgAdapter(dsn="postgresql://content_writer@db.invalid/omnidash")
    writer._db = bound

    first = writer._message_db()
    second = writer._message_db()

    assert isinstance(first, AsyncpgAdapter)
    assert isinstance(second, AsyncpgAdapter)
    assert first is not bound
    assert second is not bound
    assert first is not second
    assert first.dsn == bound.dsn == second.dsn
    assert (first._min_size, first._max_size) == (1, 1)
    assert (second._min_size, second._max_size) == (1, 1)
    assert not bound.is_connected


@pytest.mark.unit
def test_four_messages_are_written_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    writer = SessionContentProjectionWriter()
    db = _BarrierDb()
    monkeypatch.setattr(writer, "_db", db)

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [
            pool.submit(writer.handle, {**record, "_topic": TOPIC})
            for record in _records(4)
        ]
        results = [future.result() for future in futures]

    assert [result["rows_upserted"] for result in results] == [1, 1, 1, 1]
    assert len(db.statements) == 4
    assert len({args[0] for _, args in db.statements}) == 4


@pytest.mark.unit
def test_records_delivered_in_reverse_fold_to_the_same_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = _records(5)
    in_order = _RecordingDb()
    writer = SessionContentProjectionWriter()
    monkeypatch.setattr(writer, "_db", in_order)
    for record in records:
        writer.handle({**record, "_topic": TOPIC})

    reversed_order = _RecordingDb()
    writer = SessionContentProjectionWriter()
    monkeypatch.setattr(writer, "_db", reversed_order)
    for record in reversed(records):
        writer.handle({**record, "_topic": TOPIC})

    assert len(in_order.statements) == len(reversed_order.statements) == 5
    assert len({args[0] for _, args in in_order.statements}) == 5
    assert set(in_order.statements) == set(reversed_order.statements)
