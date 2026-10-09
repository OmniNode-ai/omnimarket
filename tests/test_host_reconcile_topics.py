# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Host topic declarations and the contract-addressed publisher boundary."""

import json
from pathlib import Path

import pytest
import yaml
from pydantic import BaseModel

from omnimarket.models.model_host_reconcile import (
    ModelHostReconcileRunResult,
    ModelHostReconcileSlackCommand,
)
from omnimarket.nodes.node_host_reconcile_effect.handlers import adapter_publisher
from omnimarket.nodes.node_host_reconcile_effect.handlers.adapter_publisher import (
    ContractEventPublisher,
    publish_topics,
)

pytestmark = pytest.mark.unit
NODES = Path(__file__).resolve().parents[1] / "src/omnimarket/nodes"


def test_compute_contract() -> None:
    contract = yaml.safe_load(
        (NODES / "node_host_reconcile_compute/contract.yaml").read_text()
    )
    command = contract["event_bus"]["subscribe_topics"][0]
    event = contract["event_bus"]["publish_topics"][0]
    assert command == "onex.cmd.omnimarket.host-reconcile-evaluate.v1"
    assert event == "onex.evt.omnimarket.host-reconcile-evaluated.v1"
    assert contract["event_bus"] == {
        "subscribe_topics": [command],
        "publish_topics": [event],
    }
    assert contract["runtime_dispatch"] == {"command_topic": command}
    assert contract["terminal_event"] == event
    assert contract["externally_consumed_topics"] == [event]
    assert contract["descriptor"]["purity"] == "pure"


def test_effect_contract_and_publisher_topic() -> None:
    contract = yaml.safe_load(
        (NODES / "node_host_reconcile_effect/contract.yaml").read_text()
    )
    slack, event = contract["event_bus"]["publish_topics"]
    assert slack == "onex.cmd.omnimarket.slack-publish.v1"
    assert event == "onex.evt.omnimarket.host-reconcile-run-completed.v1"
    assert contract["event_bus"] == {
        "subscribe_topics": [],
        "publish_topics": [slack, event],
    }
    assert contract["terminal_event"] == event
    assert contract["runtime_dispatch"] == {"external_trigger": True}
    assert contract["externally_consumed_topics"] == [event]
    assert contract["lifecycle"] == "experimental"
    assert contract["metadata"]["execution_locus"] == "host-local"
    assert contract["descriptor"]["timeout_ms"] == 3900000
    assert publish_topics() == (slack, event)
    assert any(
        slack
        in yaml.safe_load(path.read_text())
        .get("event_bus", {})
        .get("subscribe_topics", [])
        for path in NODES.glob("*/contract.yaml")
    )


def test_publisher_reads_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    original = Path.read_text

    def read(path: Path, encoding: str | None = None, errors: str | None = None) -> str:
        if path == Path(adapter_publisher.__file__).parent.parent / "contract.yaml":
            return "event_bus:\n  publish_topics: [command-topic, contract-topic]\n"
        return original(path, encoding=encoding, errors=errors)

    monkeypatch.setattr(Path, "read_text", read)
    assert publish_topics() == ("command-topic", "contract-topic")


def test_standalone_publisher_and_undeclared_topic(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from uuid import uuid4

    monkeypatch.delenv("KAFKA_BROKERS", raising=False)
    result = ModelHostReconcileRunResult(
        exit_code=0, host="host", correlation_id=uuid4()
    )
    publisher = ContractEventPublisher()
    with pytest.raises(ValueError, match="undeclared"):
        publisher.publish("unknown", result)
    publisher.publish(publish_topics()[1], result)
    assert json.loads(capsys.readouterr().err) == {
        "topic": publish_topics()[1],
        "event": result.model_dump(mode="json"),
    }


def test_kafka_batch_uses_host_and_correlation_keys_without_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from uuid import uuid4

    import aiokafka
    from omnibase_infra.event_bus import kafka_auth

    sent: list[tuple[str, bytes, bytes]] = []
    lifecycle: list[str] = []

    class Producer:
        def __init__(self, **kwargs: object) -> None:
            assert kwargs["bootstrap_servers"] == "stub-broker"

        async def start(self) -> None:
            lifecycle.append("start")

        async def send_and_wait(self, topic: str, value: bytes, *, key: bytes) -> None:
            sent.append((topic, value, key))

        async def stop(self) -> None:
            lifecycle.append("stop")

    monkeypatch.setattr(aiokafka, "AIOKafkaProducer", Producer)
    monkeypatch.setattr(kafka_auth, "build_aiokafka_auth_kwargs_from_env", lambda: {})
    monkeypatch.setenv("KAFKA_BROKERS", "stub-broker")
    result = ModelHostReconcileRunResult(
        exit_code=0, host="host", correlation_id=uuid4()
    )
    slack = ModelHostReconcileSlackCommand(
        channel="test-channel",
        text="failed",
        idempotency_key="test-key",
        correlation_id=result.correlation_id,
    )
    publisher = ContractEventPublisher()
    publisher.publish(publish_topics()[0], slack)
    assert sent == []
    assert lifecycle == []
    publisher.publish(publish_topics()[1], result)
    assert sent == [
        (
            publish_topics()[0],
            slack.model_dump_json().encode(),
            str(result.correlation_id).encode(),
        ),
        (publish_topics()[1], result.model_dump_json().encode(), b"host"),
    ]
    assert lifecycle == ["start", "stop"]
    assert publisher._buffer == []


def test_publisher_flushes_contract_terminal_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from uuid import uuid4

    original = Path.read_text

    def read(path: Path, encoding: str | None = None, errors: str | None = None) -> str:
        if path == Path(adapter_publisher.__file__).parent.parent / "contract.yaml":
            return (
                "event_bus:\n  publish_topics: [terminal-topic, other-topic]\n"
                "terminal_event: terminal-topic\n"
            )
        return original(path, encoding=encoding, errors=errors)

    batches: list[list[tuple[str, BaseModel]]] = []

    async def send(
        self: ContractEventPublisher, brokers: str, batch: list[tuple[str, BaseModel]]
    ) -> None:
        batches.append(batch)

    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(ContractEventPublisher, "_publish_kafka", send)
    monkeypatch.setenv("KAFKA_BROKERS", "stub-broker")
    result = ModelHostReconcileRunResult(
        exit_code=0, host="host", correlation_id=uuid4()
    )
    publisher = ContractEventPublisher()
    publisher.publish("other-topic", result)
    assert batches == []
    publisher.publish("terminal-topic", result)
    assert batches == [[("other-topic", result), ("terminal-topic", result)]]
