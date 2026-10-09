# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Lab terminals reach the existing ledger host and dedup (OMN-20278)."""

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.errors import ProjectionNotMaterializedError

from omnimarket.delegated_test_loop.lab_run_bus import (
    ProtocolBusMessage,
    event_type_for,
)
from omnimarket.lab_work.bus import load_lab_work_topics
from omnimarket.nodes.node_work_ledger_append_effect import (
    HandlerWorkLedgerAppendEffect,
)
from omnimarket.nodes.node_work_ledger_append_effect.protocols import (
    ModelAppendCommandResult,
)
from omnimarket.work_ledger_bus.bus import WorkLedgerAppendHost

pytestmark = pytest.mark.unit


class _Ledger:
    def __init__(self) -> None:
        self.rows = ""
        self.calls = 0

    def read_text(self) -> str:
        return self.rows

    def append(self, rows: str) -> ModelAppendCommandResult:
        self.calls += 1
        self.rows += rows + "\n"
        return ModelAppendCommandResult(exit_code=0, stdout="appended", stderr="")


@pytest.mark.parametrize("failed", [False, True])
def test_lab_work_receipt_redelivery_appends_once(failed: bool) -> None:
    async def scenario() -> None:
        bus = EventBusInmemory(environment="local", group="lab-ledger-test")
        await bus.start()
        ledger = _Ledger()
        host = WorkLedgerAppendHost(
            bus, HandlerWorkLedgerAppendEffect(ledger, ledger, "ledger-host")
        )
        seen: list[dict[str, object]] = []

        async def on_receipt(message: ProtocolBusMessage) -> None:
            seen.append(json.loads(message.value))

        await bus.subscribe(
            host.topics.success, on_message=on_receipt, group_id="receipt-probe"
        )
        await host.start()
        topics = load_lab_work_topics()
        topic = topics.failure if failed else topics.success
        payload = {
            "work_unit_id": "lw-0123456789abcdef",
            "host": "pool-host",
            "lane": "test-lane",
            "repo": "OmniNode-ai/omnimarket",
            "commit_sha": "a" * 40,
            "kind": "test",
            "status": "failed" if failed else "completed",
            "exit_code": None if failed else 4,
            "duration_seconds": 1.25,
        }
        envelope = ModelEventEnvelope[dict[str, object]](
            payload=payload,
            envelope_timestamp=datetime(2026, 10, 9, tzinfo=UTC),
            event_type=event_type_for(topic),
        )
        try:
            for _ in range(2):
                # Even a new transport envelope must dedup by the unit identity.
                envelope = envelope.model_copy(update={"envelope_id": uuid4()})
                await bus.publish(topic, b"unit", envelope.model_dump_json().encode())
                # A restarted serve process has no in-memory dedup state.
                await host.stop()
                host = WorkLedgerAppendHost(
                    bus, HandlerWorkLedgerAppendEffect(ledger, ledger, "ledger-host")
                )
                await host.start()
            await host.drain()
            assert ledger.calls == 1
            assert ledger.rows.count("unit=lw-0123456789abcdef") == 1
            assert len(seen) == 2
            assert [item["payload"]["status"] for item in seen] == [
                "accepted",
                "duplicate",
            ]
        finally:
            await host.stop()
            await bus.close()

    asyncio.run(scenario())


def test_append_failure_keeps_mirror_callback_failed_until_redelivery() -> None:
    class FailingLedger(_Ledger):
        fail = True

        def append(self, rows: str) -> ModelAppendCommandResult:
            if self.fail:
                return ModelAppendCommandResult(exit_code=70, stderr="disk unavailable")
            return super().append(rows)

    async def scenario() -> None:
        bus = EventBusInmemory(environment="local", group="lab-ledger-retry-test")
        await bus.start()
        ledger = FailingLedger()
        host = WorkLedgerAppendHost(
            bus, HandlerWorkLedgerAppendEffect(ledger, ledger, "ledger-host")
        )
        await host.start()
        envelope = ModelEventEnvelope[dict[str, object]](
            payload={
                "work_unit_id": "lw-retry-test",
                "host": "pool-host",
                "status": "failed",
            },
            event_type=event_type_for(load_lab_work_topics().failure),
        )
        message = SimpleNamespace(value=envelope.model_dump_json().encode())
        try:
            # This is the transport's callback seam: the callback must raise,
            # leaving the broker offset eligible for a retry.
            with pytest.raises(
                ProjectionNotMaterializedError, match="disk unavailable"
            ):
                await host._enqueue_mirror(message)
            assert ledger.rows == ""
            ledger.fail = False
            await host._enqueue_mirror(message)
            assert ledger.calls == 1
            assert ledger.rows.count("unit=lw-retry-test") == 1
            assert "lane=lab-work" in ledger.rows
        finally:
            await host.stop()
            await bus.close()

    asyncio.run(scenario())


def _mirror_message(unit: str) -> SimpleNamespace:
    envelope = ModelEventEnvelope[dict[str, object]](
        payload={"work_unit_id": unit, "host": "pool-host", "status": "failed"},
        event_type=event_type_for(load_lab_work_topics().failure),
    )
    return SimpleNamespace(value=envelope.model_dump_json().encode())


def test_refused_mirror_append_answers_once_and_releases_the_offset() -> None:
    """A grammar refusal is the row's defect: redelivery cannot fix it, so the
    callback returns (the offset advances) after one refused receipt."""

    class RefusingLedger(_Ledger):
        def append(self, rows: str) -> ModelAppendCommandResult:
            self.calls += 1
            return ModelAppendCommandResult(exit_code=65, stderr="grammar refused")

    async def scenario() -> None:
        bus = EventBusInmemory(environment="local", group="lab-ledger-refuse-test")
        await bus.start()
        ledger = RefusingLedger()
        host = WorkLedgerAppendHost(
            bus, HandlerWorkLedgerAppendEffect(ledger, ledger, "ledger-host")
        )
        seen: list[dict[str, object]] = []

        async def on_receipt(message: ProtocolBusMessage) -> None:
            seen.append(json.loads(message.value))

        await bus.subscribe(
            host.topics.success, on_message=on_receipt, group_id="receipt-probe"
        )
        await host.start()
        try:
            await host._enqueue_mirror(_mirror_message("lw-refused-test"))
            assert ledger.rows == ""
            assert [item["payload"]["status"] for item in seen] == ["refused"]
        finally:
            await host.stop()
            await bus.close()

    asyncio.run(scenario())


def test_mirror_write_path_error_publishes_no_receipt_per_retry() -> None:
    """A retried write-path error answers only the attempt that lands."""

    class FailingLedger(_Ledger):
        fail = True

        def append(self, rows: str) -> ModelAppendCommandResult:
            if self.fail:
                return ModelAppendCommandResult(exit_code=70, stderr="disk unavailable")
            return super().append(rows)

    async def scenario() -> None:
        bus = EventBusInmemory(environment="local", group="lab-ledger-noise-test")
        await bus.start()
        ledger = FailingLedger()
        host = WorkLedgerAppendHost(
            bus, HandlerWorkLedgerAppendEffect(ledger, ledger, "ledger-host")
        )
        seen: list[dict[str, object]] = []

        async def on_receipt(message: ProtocolBusMessage) -> None:
            seen.append(json.loads(message.value))

        await bus.subscribe(
            host.topics.success, on_message=on_receipt, group_id="receipt-probe"
        )
        await host.start()
        message = _mirror_message("lw-noise-test")
        try:
            for _ in range(2):
                with pytest.raises(ProjectionNotMaterializedError):
                    await host._enqueue_mirror(message)
            ledger.fail = False
            await host._enqueue_mirror(message)
            assert ledger.rows.count("unit=lw-noise-test") == 1
            assert [item["payload"]["status"] for item in seen] == ["accepted"]
        finally:
            await host.stop()
            await bus.close()

    asyncio.run(scenario())
