# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The probe tick delivers its run record on the bus its overlay declares (OMN-26043).

A lab host's unit carries no KAFKA_BOOTSTRAP_SERVERS, so the emit-effect publisher the tick defaulted to raised
KeyError at handler_event_emit_effect.py and every tick exited 3 with nothing delivered. The tick now publishes through
the shared declared broker publisher, which reads the lane from the node overlay's event_bus.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from _gateway_fake import FakeGateway

from omnimarket.handlers.bus_binding import (
    declared_broker_publisher as publisher_module,
)
from omnimarket.handlers.bus_binding.declared_broker_publisher import (
    DeclaredBrokerPublisher,
)
from omnimarket.models.bus_binding.model_node_broker import ModelNodeBroker
from omnimarket.nodes.node_delegation_check_probe_effect import (
    runtime_delegation_check_probe_tick as tick,
)
from omnimarket.nodes.node_delegation_check_probe_effect.handlers.handler_delegation_check_probe import (
    NODE,
    contract_topics,
)

pytestmark = pytest.mark.unit


def _wire(tmp: Path, monkeypatch: pytest.MonkeyPatch, *, event_bus: bool) -> None:
    doc: dict[str, object] = {
        "checks": ["C17"],
        "history_file": "runs.jsonl",
        "history_keep": 5,
        "result_topic": contract_topics()[0],
        "responder_timeout_s": 1,
    }
    if event_bus:
        doc["event_bus"] = {"type": "kafka", "lane": "dev"}
    probe = tmp / "probe.yaml"
    probe.write_text(yaml.safe_dump(doc))
    row = tmp / "responders.yaml"
    row.write_text(
        yaml.safe_dump({"schema_version": 1, "responders": [{"tier": "openrouter"}]})
    )
    monkeypatch.setenv(tick.OVERLAY_FILE_ENV, str(probe))
    monkeypatch.setenv(tick.RESPONDERS_FILE_ENV, str(row))
    monkeypatch.delenv("KAFKA_BOOTSTRAP_SERVERS", raising=False)


def _fake_producer(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[tuple[str, bytes, bytes | None]], list[str]]:
    import aiokafka

    sent: list[tuple[str, bytes, bytes | None]] = []

    class Producer:
        def __init__(self, **kwargs: object) -> None: ...

        async def start(self) -> None: ...

        async def stop(self) -> None: ...

        async def send_and_wait(
            self, topic: str, value: bytes, key: bytes | None = None
        ) -> None:
            sent.append((topic, value, key))

    monkeypatch.setattr(aiokafka, "AIOKafkaProducer", Producer)
    asked: list[str] = []

    def _resolve(node: str) -> ModelNodeBroker:
        asked.append(node)
        return ModelNodeBroker(
            kind="kafka", lane="dev", bootstrap_servers="broker.invalid:9092"
        )

    monkeypatch.setattr(publisher_module, "resolve_dialable_broker", _resolve)
    return sent, asked


def test_a_tick_with_no_kafka_bootstrap_delivers_on_the_overlay_declared_broker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _wire(tmp_path, monkeypatch, event_bus=True)
    sent, asked = _fake_producer(monkeypatch)
    code = tick.main(
        ["--omni-home", str(tmp_path), "--state-dir", str(tmp_path / "state")],
        gateway=FakeGateway(),
    )
    assert code != tick.EXIT_NOT_PUBLISHED
    assert len(sent) == 1
    topic, value, key = sent[0]
    assert topic.endswith(contract_topics()[0])
    record = json.loads(value)
    assert record["criterion"] == "C17"
    assert record["check_id"] == "C17"
    assert "payload" not in record
    assert key == b"C17"
    assert asked == [NODE]


def test_a_flat_publisher_sends_the_payload_alone_and_the_default_keeps_the_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The result topic's other records (the emit effect's wire shape, read on the dev broker from h051) carry check_id
    and verdict at the top level; a record wrapped as {event_type, event_id, correlation_id, payload} hides them from
    that reader. The default envelope stays for the callers that already publish it."""
    sent, _ = _fake_producer(monkeypatch)
    row = {"topic": "t", "event_type": "e", "payload": {"check_id": "C17"}}
    assert DeclaredBrokerPublisher(NODE, flat=True).publish(event_id="r", **row)
    assert DeclaredBrokerPublisher(NODE).publish(event_id="r", **row)
    assert json.loads(sent[0][1]) == {"check_id": "C17"}
    assert set(json.loads(sent[1][1])) == {
        "event_type",
        "event_id",
        "correlation_id",
        "payload",
    }


def test_a_tick_whose_overlay_declares_no_bus_still_exits_not_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _wire(tmp_path, monkeypatch, event_bus=False)
    monkeypatch.setenv("OMNI_HOME", str(tmp_path))
    monkeypatch.setenv("ONEX_WORKSPACE_CONFIG_ROOT", str(tmp_path))
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path / "no-roots"))
    code = tick.main(
        ["--omni-home", str(tmp_path), "--state-dir", str(tmp_path / "state")],
        gateway=FakeGateway(),
    )
    assert code == tick.EXIT_NOT_PUBLISHED
