# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The serve host appends a landing decision's rows once (OMN-17427)."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

from omnimarket.delegated_test_loop.lab_run_bus import event_type_for
from omnimarket.nodes.node_work_ledger_append_effect import (
    HandlerWorkLedgerAppendEffect,
)
from omnimarket.nodes.node_work_ledger_append_effect.protocols import (
    LocalLedgerFile,
    ModelAppendCommandResult,
)
from omnimarket.work_ledger_bus.bus import WorkLedgerAppendHost

pytestmark = pytest.mark.unit

HEAD = "b" * 40


class _Runner:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.calls: list[str] = []

    def append(self, rows: str) -> ModelAppendCommandResult:
        self.calls.append(rows)
        with self.path.open("a") as stream:
            stream.write(rows + "\n")
        return ModelAppendCommandResult(exit_code=0)


def _host(bus: EventBusInmemory, runner: _Runner, path: Path) -> WorkLedgerAppendHost:
    return WorkLedgerAppendHost(
        bus,
        HandlerWorkLedgerAppendEffect(
            runner,
            LocalLedgerFile(path),
            "ledger",
            writer_principals=["operator"],
            operator_principal="operator",
        ),
        writer_principals=["operator"],
        operator_principal="operator",
    )


def _decision(actions: list[dict[str, object]]) -> dict[str, object]:
    return {"tick": 4, "actions": actions, "next_state": {}}


def test_decision_event_redelivery_appends_its_rows_once(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = tmp_path / "ledger.md"
        path.write_text("# Ledger\n")
        runner = _Runner(path)
        bus = EventBusInmemory(environment="local", group="landing-ledger-test")
        await bus.start()
        host = _host(bus, runner, path)
        await host.start()
        try:
            topic = host.landing_topics[0]
            assert topic == "onex.evt.omnimarket.pr-landing-decision-completed.v1"
            event = uuid4()
            envelope = ModelEventEnvelope[dict[str, object]](
                payload=_decision(
                    [
                        {
                            "kind": "merge",
                            "subject": "OmniNode-ai/repo#5",
                            "head_sha": HEAD,
                        },
                        {
                            "kind": "rerun",
                            "subject": "OmniNode-ai/repo#6",
                            "head_sha": HEAD,
                        },
                    ]
                ),
                correlation_id=event,
                event_type=event_type_for(topic),
            )
            for _ in range(2):
                envelope = envelope.model_copy(update={"envelope_id": uuid4()})
                await bus.publish(
                    topic, str(event).encode(), envelope.model_dump_json().encode()
                )
                await host.drain()
            assert len(runner.calls) == 1
            text = path.read_text()
            assert text.count("action=merge") == 1
            assert text.count("action=rerun") == 1
            assert text.count("src=landing") == 2
        finally:
            await host.stop()
            await bus.close()

    asyncio.run(scenario())


def test_idle_tick_appends_nothing(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = tmp_path / "ledger.md"
        path.write_text("# Ledger\n")
        runner = _Runner(path)
        bus = EventBusInmemory(environment="local", group="landing-ledger-idle")
        await bus.start()
        host = _host(bus, runner, path)
        await host.start()
        try:
            topic = host.landing_topics[0]
            envelope = ModelEventEnvelope[dict[str, object]](
                payload=_decision([]),
                correlation_id=uuid4(),
                event_type=event_type_for(topic),
            )
            await host._enqueue_landing(
                SimpleNamespace(value=envelope.model_dump_json().encode()),
                landing_topic=topic,
            )
            assert runner.calls == []
            assert path.read_text() == "# Ledger\n"
        finally:
            await host.stop()
            await bus.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("invalid_kind", ["no-correlation", "malformed", "bad-tick"])
def test_invalid_decision_is_answered_without_appending(
    tmp_path: Path, invalid_kind: str
) -> None:
    async def scenario() -> None:
        path = tmp_path / "ledger.md"
        path.write_text("# Ledger\n")
        runner = _Runner(path)
        bus = EventBusInmemory(environment="local", group="landing-ledger-bad")
        await bus.start()
        host = _host(bus, runner, path)
        await host.start()
        try:
            topic = host.landing_topics[0]
            if invalid_kind == "malformed":
                value = b"not json"
            else:
                payload = _decision([])
                if invalid_kind == "bad-tick":
                    payload["tick"] = 0
                envelope = ModelEventEnvelope[dict[str, object]](
                    payload=payload,
                    correlation_id=uuid4(),
                    event_type=event_type_for(topic),
                )
                dumped = envelope.model_dump(mode="json")
                if invalid_kind == "no-correlation":
                    dumped["correlation_id"] = None

                value = json.dumps(dumped).encode()
            await host._enqueue_landing(
                SimpleNamespace(value=value), landing_topic=topic
            )
            assert runner.calls == []
        finally:
            await host.stop()
            await bus.close()

    asyncio.run(scenario())
