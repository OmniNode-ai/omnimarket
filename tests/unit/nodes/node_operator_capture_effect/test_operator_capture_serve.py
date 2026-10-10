# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Operator prompts off the bus become ledger rows; lane briefs become none (OMN-20905).

RULING 2026-10-10T22:40:43Z: the capture runs async off the bus, no capture hooks. The serve host
reads the content-capture topic over the in-memory bus here; the model and the ledger are
injected ports.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

from omnimarket.delegated_test_loop.lab_run_bus import ProtocolBusMessage
from omnimarket.models.operator_capture import (
    OPERATOR_PROMPT_KIND,
    EnumOperatorCaptureStatus,
    ModelOperatorPromptRecord,
)
from omnimarket.nodes.node_operator_capture_effect.handlers.capture_ports import (
    DelegateAnswer,
    runner_ledger_appender,
)
from omnimarket.nodes.node_operator_capture_effect.handlers.handler_capture_serve import (
    OperatorCaptureHost,
    capture_group_id,
    ledger_host_capture,
    load_operator_capture_topics,
)
from omnimarket.nodes.node_operator_capture_effect.handlers.handler_operator_capture_effect import (
    HandlerOperatorCaptureEffect,
)
from omnimarket.nodes.node_work_ledger_append_effect.protocols import (
    LocalLedgerAppendCommand,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 10, 10, 23, 0, 0, tzinfo=UTC)
WORDS = "We are not building capture hooks. Can you move the capture onto the bus?"
ANSWER = json.dumps(
    {
        "items": [
            {
                "kind": "decision",
                "quote": "We are not building capture hooks.",
                "subject": "capture hooks",
                "confidence": 0.95,
            },
            {
                "kind": "ask",
                "quote": "Can you move the capture onto the bus?",
                "subject": "capture on the bus",
                "confidence": 0.9,
            },
        ]
    }
)


class FakeLedger:
    def __init__(self) -> None:
        self.rows: list[str] = []

    def __call__(self, row: str) -> tuple[bool, str]:
        self.rows.append(row)
        return True, "ok"


def _delegate(prompt: str, contract: Mapping[str, Any]) -> DelegateAnswer:
    return DelegateAnswer(ANSWER, "Qwen3.8-27B", None)


def _handler(tmp_path: Path, append: FakeLedger) -> HandlerOperatorCaptureEffect:
    ledger = tmp_path / "ROLLING_WORK_LEDGER.md"
    ledger.touch()
    return HandlerOperatorCaptureEffect(
        store_dir=tmp_path / "store",
        ledger_path=ledger,
        host_name="h-ledger",
        delegate=_delegate,
        append=append,
        clock=lambda: NOW,
    )


def _record(**kw: Any) -> ModelOperatorPromptRecord:
    fields: dict[str, Any] = {
        "session_id": "sess-op",
        "content_kind": OPERATOR_PROMPT_KIND,
        "content": WORDS,
        "prompt_id": "prompt-1",
        "said_at": "2026-10-10T22:59:00+00:00",
    }
    fields.update(kw)
    return ModelOperatorPromptRecord(**fields)


def test_an_operator_prompt_becomes_rows_with_the_verbatim_words(
    tmp_path: Path,
) -> None:
    append = FakeLedger()
    receipt = _handler(tmp_path, append).handle(_record())
    assert receipt.status is EnumOperatorCaptureStatus.RECORDED
    assert (receipt.host, receipt.session_id, receipt.prompt_id) == (
        "h-ledger",
        "sess-op",
        "prompt-1",
    )
    decision, ask = append.rows
    assert decision.startswith("2026-10-10T23:00:00Z | STATUS | lane=operator-capture")
    assert '"We are not building capture hooks."' in decision
    assert "said=2026-10-10T22:59:00Z" in decision
    assert "kind=ask" in ask
    assert "state=open" in ask
    assert "source=bus:claude" in ask


def test_a_redelivered_prompt_is_recorded_once(tmp_path: Path) -> None:
    append = FakeLedger()
    handler = _handler(tmp_path, append)
    handler.handle(_record())
    again = handler.handle(_record())
    assert again.status is EnumOperatorCaptureStatus.DUPLICATE
    assert len(append.rows) == 2


def test_a_record_that_is_not_an_operator_prompt_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not an operator prompt"):
        _handler(tmp_path, FakeLedger()).handle(_record(content_kind="prompt"))


def test_a_said_at_the_fan_out_hashed_falls_back_to_the_emit_time(
    tmp_path: Path,
) -> None:
    append = FakeLedger()
    _handler(tmp_path, append).handle(
        _record(said_at="sha256:" + "0" * 64, emitted_at="2026-10-10T22:58:00Z")
    )
    assert "said=2026-10-10T22:58:00Z" in append.rows[0]


# -- the serve host over the in-memory bus ----------------------------------------------------


class _Bus(EventBusInmemory):
    def __init__(self) -> None:
        super().__init__(environment="local", group="operator-capture-test")
        self.groups: list[tuple[str, str]] = []

    async def subscribe(
        self,
        topic: str,
        on_message: Callable[[Any], Awaitable[None]],
        group_id: str,
    ) -> Callable[[], Awaitable[None]]:
        self.groups.append((topic, group_id))
        return await super().subscribe(topic, on_message=on_message, group_id=group_id)


async def _publish(bus: _Bus, payload: dict[str, Any]) -> None:
    topic = load_operator_capture_topics().prompts
    envelope = ModelEventEnvelope[dict[str, object]](
        payload=payload, event_type="content.captured"
    )
    await bus.publish(
        topic, str(payload["session_id"]).encode(), envelope.model_dump_json().encode()
    )


async def _wait_for(pred: Callable[[], bool], timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


def test_the_host_records_the_operator_prompt_and_nothing_a_lane_said(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        bus = _Bus()
        await bus.start()
        append = FakeLedger()
        host = OperatorCaptureHost(bus, _handler(tmp_path, append), retry_seconds=0)
        receipts: list[dict[str, Any]] = []

        async def on_receipt(message: ProtocolBusMessage) -> None:
            receipts.append(json.loads(message.value)["payload"])

        await bus.subscribe(host.topics.receipt, on_receipt, "probe")
        await host.start()
        try:
            await _publish(
                bus,
                {
                    "session_id": "sess-lane",
                    "content_kind": "prompt",
                    "content": "You are a headless lane. Build X.",
                },
            )
            await _publish(
                bus,
                {
                    "session_id": "sess-lane",
                    "content_kind": "tool_input",
                    "content": "{}",
                },
            )
            await _publish(bus, _record().model_dump(mode="json"))
            await _wait_for(lambda: len(receipts) == 1)
            assert receipts[0]["status"] == "recorded"
            assert receipts[0]["result"]["rows_appended"] == 2
            assert len(append.rows) == 2
            assert all("session=sess-op" in r for r in append.rows)
            assert host.skipped == 2
            assert (host.topics.prompts, capture_group_id()) in bus.groups
        finally:
            await host.stop()
            await bus.close()

    asyncio.run(scenario())


def _file_append_command(ledger: Path) -> LocalLedgerAppendCommand:
    """An append command shaped like the ledger's own: ``<argv> --append <row> --timeout <s>``."""
    return LocalLedgerAppendCommand(
        [
            sys.executable,
            "-c",
            "import sys; from pathlib import Path; "
            "p = Path(sys.argv[1]); "
            "p.write_text(p.read_text() + sys.argv[3] + '\\n')",
            str(ledger),
        ]
    )


def test_a_failed_runner_append_is_reported_not_swallowed(tmp_path: Path) -> None:
    refuse = LocalLedgerAppendCommand(
        [sys.executable, "-c", "import sys; sys.stderr.write('locked'); sys.exit(75)"]
    )
    ok, detail = runner_ledger_appender(refuse)("2026-10-10T23:00:00Z | MSG | x")
    assert not ok
    assert "exit 75" in detail
    assert "locked" in detail


def test_the_ledger_host_capture_appends_through_the_serve_process_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The work-ledger serve process's own append runner writes the rows (OMN-20905)."""
    monkeypatch.setenv("ONEX_OPERATOR_CAPTURE_DIR", str(tmp_path / "store"))
    ledger = tmp_path / "ROLLING_WORK_LEDGER.md"
    ledger.write_text("# Ledger\n")

    async def scenario() -> None:
        bus = _Bus()
        await bus.start()
        # No bus lane: no delegation, so the deterministic fallback classifies.
        host = ledger_host_capture(
            bus,
            ledger=ledger,
            append_runner=_file_append_command(ledger),
            host_name="h-ledger",
            bus_lane=None,
            retry_seconds=0,
        )
        await host.start()
        try:
            await _publish(
                bus,
                {
                    "session_id": "sess-lane",
                    "content_kind": "prompt",
                    "content": "Build lane X. Return under 120 words.",
                },
            )
            await _publish(bus, _record().model_dump(mode="json"))
            await _wait_for(lambda: host.handled == 1, timeout=10.0)
        finally:
            await host.stop()
            await bus.close()

    asyncio.run(scenario())
    rows = ledger.read_text().splitlines()[1:]
    assert rows, "no row was appended"
    assert all("session=sess-op" in row for row in rows)
    assert not any("sess-lane" in row or "Build lane X" in row for row in rows)
    assert any('"Can you move the capture onto the bus?"' in row for row in rows)
