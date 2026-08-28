# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-16874 / OMN-16875 — the writer's dispatch contract with the runtime.

``ConsumerFlowProjectionWriter`` is dispatched **in-process** by the runtime's
auto-wiring, once per consumed message, through a synchronous ``handle()`` that
opens its own event loop with ``asyncio.run``. Two obligations follow from that,
and both were unmet on the ``.201`` dev lane on 2026-08-28:

1. **Every loop-bound resource is scoped to that one loop.** ``asyncio.run``
   closes the loop it opened, so an asyncpg pool (or an ``AIOKafkaProducer``)
   cached across calls belongs to a loop that no longer exists. Live: 34
   ``RuntimeError: Event loop is closed``, ``consumer_flow_windows`` at 0 rows,
   DLQ at 36 and climbing. Two consecutive messages are driven here because one
   cannot expose this.

2. **The return value is the applied event's payload.** ``handler_wiring``
   publishes the handler's result onto
   ``onex.evt.omnimarket.projection-consumer-flow-applied.v1`` (OMN-16875), so a
   bare ``{"projected": True}`` ack discarded every window fact at the producer
   and left the downstream stall alert with nothing to evaluate.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest

from omnimarket.nodes.node_projection_consumer_flow.handlers.handler_consumer_flow_runner import (
    ConsumerFlowProjectionWriter,
)

_T0 = datetime(2026, 8, 27, 12, 0, 0, tzinfo=UTC)
_IN_TOPIC = "onex.evt.platform.node-heartbeat.v1"  # onex-topic-allow: real topic from the OMN-16755 incident
_STALLED_GROUP = "local.omnimarket.node_registration_orchestrator.consume"


class _LoopBoundPool:
    """Reduced asyncpg: usable only from the loop that created it."""

    def __init__(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.closed = False

    def check(self) -> None:
        if self.closed:
            raise RuntimeError("pool is closed")
        if asyncio.get_running_loop() is not self.loop:
            raise RuntimeError("Event loop is closed")


class _RecordingAdapter:
    """Stands in for ``AsyncpgAdapter`` and enforces its loop affinity."""

    def __init__(self) -> None:
        self._pool: _LoopBoundPool | None = None
        self.connects = 0
        self.rows: list[tuple[str, tuple[Any, ...]]] = []

    async def connect(self) -> None:
        self._pool = _LoopBoundPool()
        self.connects += 1

    async def close(self) -> None:
        if self._pool is not None:
            self._pool.closed = True
            self._pool = None

    async def execute(self, query: str, *params: Any) -> list[dict[str, Any]]:
        assert self._pool is not None, "call connect() first"
        self._pool.check()
        self.rows.append((query, params))
        if "SELECT COUNT(*)" in query:
            return [{"window_count": 0, "produced": 0}]
        if "MAX(ingest_sequence)" in query:
            return [{"last_sequence": None}]
        if "RETURNING" in query:
            return [_written_row()]
        return []


def _written_row() -> dict[str, Any]:
    return {
        "consumer_group": _STALLED_GROUP,
        "topic": _IN_TOPIC,
        "window_start": _T0,
        "window_end": _T0 + timedelta(seconds=60),
        "node_id": str(_NODE_ID),
        "ingest_sequence": 1,
        "messages_in": 229150,
        "messages_out": 0,
        "messages_dlq": 0,
        "handler_errors": 0,
        "upstream_produced": None,
        "upstream_evidence": "UNOBSERVED",
        "flow_state": "STALLED",
        "evaluated_at": _T0 + timedelta(seconds=60),
    }


_NODE_ID = uuid4()


def _heartbeat(sequence: int) -> dict[str, Any]:
    start = _T0 + timedelta(seconds=60 * sequence)
    return {
        "flow_window": {
            "node_id": str(_NODE_ID),
            "window_start": start.isoformat(),
            "window_end": (start + timedelta(seconds=60)).isoformat(),
            "window_sequence": sequence,
            "consumer_deltas": [
                {
                    "consumer_group": _STALLED_GROUP,
                    "topic": _IN_TOPIC,
                    "node_id": str(_NODE_ID),
                    "window_start": start.isoformat(),
                    "window_end": (start + timedelta(seconds=60)).isoformat(),
                    "window_sequence": sequence,
                    "messages_in": 229150,
                    "messages_out": 0,
                    "messages_dlq": 0,
                    "handler_errors": 0,
                }
            ],
            "produce_deltas": [],
        },
        "_topic": _IN_TOPIC,
    }


@pytest.fixture
def writer(monkeypatch: pytest.MonkeyPatch) -> ConsumerFlowProjectionWriter:
    monkeypatch.setenv("OMNIDASH_ANALYTICS_DB_URL", "postgresql://fixture/db")
    instance = ConsumerFlowProjectionWriter()
    instance._db = _RecordingAdapter()  # type: ignore[assignment]
    # The snapshot publish is a separate seam with its own transport; this test
    # is about the DB write path and the returned facts.
    instance._snapshot_exposure = None
    return instance


@pytest.mark.unit
def test_writer_declares_the_in_process_dispatch_capability() -> None:
    """The runtime routes on a declared capability, never on the class name.

    OMN-16874: ``handler_wiring._is_standalone_projection_runner`` used to test
    ``type(h).__name__.endswith("ProjectionRunner")``. This class is named
    ``...Writer`` because the OMN-14350 type-word ratchet hard-fails ``Runner``,
    so it silently fell out of that gate onto the other dispatch branch. The
    intent is now stated rather than inferred from a spelling.
    """
    assert ConsumerFlowProjectionWriter.onex_runtime_inprocess_dispatch is True


@pytest.mark.unit
def test_no_sibling_projection_runner_claims_in_process_dispatch() -> None:
    """Exactly one runner-shaped class opts in, and every sibling stays out.

    The capability is what moves a class between the runtime's two projection
    dispatch branches, so a stray declaration on a ``*ProjectionRunner`` would
    double-dispatch a projection that already runs in-process through its own
    pure handler — writing every row twice. This walks the real contracts rather
    than a hand-kept list, so a new projection node is covered the day it lands.
    """
    import importlib
    import pathlib

    import yaml

    attr = "onex_runtime_inprocess_dispatch"
    nodes = pathlib.Path(__file__).parent.parent / "src/omnimarket/nodes"
    declared: set[str] = set()
    checked = 0

    for node_dir in sorted(nodes.glob("node_projection_*")):
        contract = node_dir / "contract.yaml"
        if not contract.exists():
            continue
        routing = yaml.safe_load(contract.read_text()).get("handler_routing") or {}
        for entry in routing.get("handlers", []):
            ref = entry.get("handler") or {}
            name, module = ref.get("name"), ref.get("module")
            if not name or not module:
                continue
            klass = getattr(importlib.import_module(module), name, None)
            if klass is None:
                continue
            checked += 1
            if getattr(klass, attr, False):
                declared.add(name)

    assert checked > 0, "the contract walk found no handlers — the test is inert"
    assert declared == {"ConsumerFlowProjectionWriter"}, (
        f"unexpected {attr} declarations: {sorted(declared)}"
    )


@pytest.mark.unit
def test_two_consecutive_messages_both_write(
    writer: ConsumerFlowProjectionWriter,
) -> None:
    """A second real write must not die on a loop the first one closed."""
    first = writer.handle(_heartbeat(1))
    second = writer.handle(_heartbeat(2))

    adapter = writer.db
    assert adapter.connects == 2, (  # type: ignore[attr-defined]
        "each dispatch opens its own loop, so each must open and close its own "
        "pool inside it"
    )
    assert first["rows_upserted"] >= 1
    assert second["rows_upserted"] >= 1


@pytest.mark.unit
def test_applied_payload_carries_the_window_facts_not_a_bare_ack(
    writer: ConsumerFlowProjectionWriter,
) -> None:
    """OMN-16875 AC1: the returned result IS the applied event's payload.

    ``handler_wiring`` publishes this dict onto the applied topic, so the facts
    the downstream stall alert needs must be present here. A bare
    ``{"projected": True}`` discards them at the producer, where no consumer can
    reconstruct them.
    """
    result = writer.handle(_heartbeat(1))

    assert result["rows_upserted"] == 1
    rows = result["flow_rows"]
    assert isinstance(rows, list)
    assert len(rows) == 1
    row = rows[0]
    for field in (
        "consumer_group",
        "topic",
        "window_start",
        "window_end",
        "messages_in",
        "messages_out",
        "messages_dlq",
        "flow_state",
    ):
        assert field in row, f"applied payload dropped {field!r}"
    assert row["consumer_group"] == _STALLED_GROUP
    assert row["messages_in"] == 229150
    assert row["messages_out"] == 0
    assert row["flow_state"] == "STALLED"


@pytest.mark.unit
def test_a_heartbeat_with_no_window_reports_zero_rows(
    writer: ConsumerFlowProjectionWriter,
) -> None:
    """A priming tick carries no window: nothing is written, and it says so.

    This path used to return ``True``, which ``_extract_rows_upserted`` mapped to
    one row and the runtime published as a successful projection. Three such
    acks on the dev lane were read as "the first message of each boot
    succeeded"; no message had ever written anything.
    """
    result = writer.handle({"_topic": _IN_TOPIC})

    assert result["rows_upserted"] == 0
    assert result["flow_rows"] == []
