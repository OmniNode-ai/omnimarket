# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One asyncpg pool per writer, opened once and reused (OMN-23257).

The claude_hook_events and session_content writers, and the beacon trace
readback, opened an asyncpg connection pool for every message (or every poll).
These tests drive the REAL ``AsyncpgAdapter`` over a fake ``asyncpg.create_pool``
and count how many pools open for 100 messages. The count is the reading: it is
100 on the per-message code and 1 with a kept pool.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from uuid import uuid4

import asyncpg
import pytest

from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter
from omnimarket.nodes.node_beacon_trace_capture_effect.handlers.adapter_io import (
    ProjectionReadback,
)
from omnimarket.nodes.node_projection_claude_hook_events.handlers.handler_claude_hook_events_writer import (
    ClaudeHookEventsProjectionWriter,
)
from omnimarket.nodes.node_projection_session_content.handlers.handler_session_content import (
    SessionContentProjectionWriter,
)

MESSAGES = 100
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "claude_hook_capture"
HOOK_TOPIC = "onex.evt.omniclaude.hook-event.v1"
CONTENT_TOPIC = "onex.cmd.omniintelligence.content-captured.v1"
DSN = "postgresql://writer@db.invalid/omnidash"


class _Transaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: object) -> None:
        return None


class _Connection:
    def __init__(self, pool: _FakePool) -> None:
        self._pool = pool

    def transaction(self) -> _Transaction:
        return _Transaction()

    async def execute(self, *args: Any) -> str:
        return "OK"

    async def fetch(self, query: str, *params: Any) -> list[dict[str, Any]]:
        self._pool.fetches += 1
        self._pool.owner.fetches += 1
        if self._pool.gate is not None:
            await asyncio.to_thread(self._pool.gate.wait)
        return self._pool.rows_for(query, self._pool.owner.fetches)


class _Acquire:
    def __init__(self, pool: _FakePool) -> None:
        self._pool = pool

    async def __aenter__(self) -> _Connection:
        self._pool.in_use += 1
        self._pool.peak_in_use = max(self._pool.peak_in_use, self._pool.in_use)
        return _Connection(self._pool)

    async def __aexit__(self, *exc: object) -> None:
        self._pool.in_use -= 1


class _FakePool:
    def __init__(self, owner: _Pools, gate: threading.Event | None = None) -> None:
        self.owner = owner
        self.gate = gate
        self.fetches = 0
        self.in_use = 0
        self.peak_in_use = 0
        self.closed = False
        self.event_ids: tuple[Any, ...] = ()
        self.visible_after = 0

    def rows_for(self, query: str, nth: int) -> list[dict[str, Any]]:
        if (
            "omninode_internal.claude_hook_events" in query
            and "event_id = any" in query
        ):
            if nth >= self.visible_after:
                return [{"event_id": event_id} for event_id in self.event_ids]
            return []
        return [{"event_inserted": True, "parent_resolution": "main", "agent_id": "a"}]

    def acquire(self, timeout: float | None = None) -> _Acquire:
        return _Acquire(self)

    async def close(self) -> None:
        self.closed = True


class _Pools:
    """Count what the writers ask asyncpg to open."""

    def __init__(self, gate: threading.Event | None = None) -> None:
        self.opened: list[_FakePool] = []
        self.sizes: list[tuple[int, int]] = []
        self.gate = gate
        self.fetches = 0

    async def create_pool(self, dsn: str, **kwargs: Any) -> _FakePool:
        pool = _FakePool(self, self.gate)
        self.opened.append(pool)
        self.sizes.append((kwargs["min_size"], kwargs["max_size"]))
        return pool


@pytest.fixture
def pools(monkeypatch: pytest.MonkeyPatch) -> _Pools:
    fake = _Pools()
    monkeypatch.setattr(asyncpg, "create_pool", fake.create_pool)
    return fake


def _hook_message(index: int) -> dict[str, Any]:
    event = json.loads((FIXTURES / "events" / "PreToolUse.json").read_text("utf-8"))
    event["event_id"] = str(uuid4())
    message: dict[str, Any] = json.loads(json.dumps(event))
    message.update({"_topic": HOOK_TOPIC, "_partition": 0, "_offset": index})
    return message


def _content_message(index: int) -> dict[str, Any]:
    content = f"content record {index}"
    return {
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
        "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "original_chars": len(content),
        "truncated": False,
        "producer_redaction": {},
        "content": content,
        "command": None,
        "redaction_state": "clean",
        "lane": "writer-pool-test",
        "_topic": CONTENT_TOPIC,
        "_partition": 0,
        "_offset": index,
    }


def _hook_writer(**kwargs: Any) -> ClaudeHookEventsProjectionWriter:
    writer = ClaudeHookEventsProjectionWriter(**kwargs)
    writer._db = AsyncpgAdapter(dsn=DSN)
    return writer


def _content_writer(**kwargs: Any) -> SessionContentProjectionWriter:
    writer = SessionContentProjectionWriter(**kwargs)
    writer._db = AsyncpgAdapter(dsn=DSN)
    return writer


def test_writer_pool_claude_hook_events_connects_once_for_100_messages(
    pools: _Pools,
) -> None:
    writer = _hook_writer()
    try:
        for index in range(MESSAGES):
            result = writer.handle(_hook_message(index))
            assert result["rows_upserted"] >= 1
    finally:
        if hasattr(writer, "close_pool"):
            writer.close_pool()
    assert len(pools.opened) == 1, f"{len(pools.opened)} pools opened for {MESSAGES}"


def test_writer_pool_session_content_connects_once_for_100_messages(
    pools: _Pools,
) -> None:
    writer = _content_writer()
    try:
        for index in range(MESSAGES):
            result = writer.handle(_content_message(index))
            assert result["rows_upserted"] == 1
    finally:
        if hasattr(writer, "close_pool"):
            writer.close_pool()
    assert len(pools.opened) == 1, f"{len(pools.opened)} pools opened for {MESSAGES}"


def test_writer_pool_concurrent_messages_share_the_one_pool(pools: _Pools) -> None:
    """The contract's four in flight run on one pool and never reopen it."""
    writer = _hook_writer()
    try:
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(
                executor.map(
                    writer.handle, (_hook_message(index) for index in range(MESSAGES))
                )
            )
    finally:
        if hasattr(writer, "close_pool"):
            writer.close_pool()
    assert len(results) == MESSAGES
    assert len(pools.opened) == 1
    assert pools.opened[0].closed


def test_writer_pool_sizes_come_from_the_node_contracts(pools: _Pools) -> None:
    """No size is typed into the writer: it reads its contract's writer_pool."""
    import yaml

    for node, writer, message in (
        (
            "node_projection_claude_hook_events",
            _hook_writer(),
            _hook_message(0),
        ),
        (
            "node_projection_session_content",
            _content_writer(),
            _content_message(0),
        ),
    ):
        contract = yaml.safe_load(
            (
                Path(__file__).resolve().parents[1]
                / "src/omnimarket/nodes"
                / node
                / "contract.yaml"
            ).read_text("utf-8")
        )
        declared = contract["writer_pool"]
        writer.handle(message)
        writer.close_pool()
        assert pools.sizes[-1] == (declared["min_size"], declared["max_size"])


def test_writer_pool_beacon_readback_connects_once_across_polls(
    pools: _Pools, monkeypatch: pytest.MonkeyPatch
) -> None:
    event_ids = (uuid4(),)
    created = pools.create_pool

    async def create_pool(dsn: str, **kwargs: Any) -> _FakePool:
        pool = await created(dsn, **kwargs)
        pool.event_ids = event_ids
        pool.visible_after = MESSAGES
        return pool

    monkeypatch.setattr(asyncpg, "create_pool", create_pool)
    readback = ProjectionReadback(dsn=DSN)
    rows = asyncio.run(
        readback.read_until_visible(
            event_ids, poll_interval_seconds=0, deadline_seconds=10
        )
    )
    assert len(rows) == 1
    assert pools.fetches == MESSAGES
    assert len(pools.opened) == 1, (
        f"{len(pools.opened)} pools opened for {MESSAGES} polls"
    )


def test_writer_pool_failure_closed_pool_raises_typed_and_never_reconnects(
    pools: _Pools,
) -> None:
    from omnimarket.handlers.writer_pool_host import WriterPoolClosedError

    for writer, message in (
        (_hook_writer(), _hook_message(0)),
        (_content_writer(), _content_message(0)),
    ):
        before = len(pools.opened)
        writer.handle(message)
        writer.close_pool()
        for _ in range(3):
            with pytest.raises(WriterPoolClosedError):
                writer.handle(message)
        assert len(pools.opened) == before + 1


def test_writer_pool_failure_beacon_readback_after_close_raises_typed(
    pools: _Pools,
) -> None:
    from omnimarket.handlers.writer_pool_host import WriterPoolClosedError

    async def scenario() -> None:
        readback = ProjectionReadback(dsn=DSN)
        await readback.read((uuid4(),))
        await readback.close()
        with pytest.raises(WriterPoolClosedError):
            await readback.read((uuid4(),))

    asyncio.run(scenario())
    assert len(pools.opened) == 1


def test_writer_pool_failure_exhausted_pool_raises_typed_without_new_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnimarket.handlers.writer_pool_host import WriterPoolExhaustedError
    from omnimarket.models.model_writer_pool_settings import ModelWriterPoolSettings

    gate = threading.Event()
    fake = _Pools(gate)
    monkeypatch.setattr(asyncpg, "create_pool", fake.create_pool)
    settings = ModelWriterPoolSettings(
        min_size=1, max_size=2, acquire_timeout_seconds=0.3, command_timeout_seconds=5
    )
    writer = _hook_writer(pool_settings=settings)
    try:
        with ThreadPoolExecutor(max_workers=3) as executor:
            held = [executor.submit(writer.handle, _hook_message(i)) for i in range(2)]
            while fake.opened == [] or fake.opened[0].in_use < 2:
                threading.Event().wait(0.01)
            with pytest.raises(WriterPoolExhaustedError):
                writer.handle(_hook_message(9))
            gate.set()
            assert [future.result()["rows_upserted"] >= 1 for future in held] == [
                True,
                True,
            ]
        assert len(fake.opened) == 1
        assert fake.opened[0].peak_in_use == 2
    finally:
        gate.set()
        writer.close_pool()
