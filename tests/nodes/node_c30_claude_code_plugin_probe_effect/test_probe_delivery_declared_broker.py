# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The C30 probe tick delivers its run record on the bus its overlay declares (OMN-19915).

A lab host's unit carries no KAFKA_BOOTSTRAP_SERVERS, so the emit-effect publisher the tick defaulted to could not deliver
and the tick exited 3. The tick now publishes through the shared declared broker publisher, which reads the lane from the
node overlay's event_bus, as the delegation check probe tick does.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.bus_binding import (
    declared_broker_publisher as publisher_module,
)
from omnimarket.models.bus_binding.model_node_broker import ModelNodeBroker
from omnimarket.nodes.node_c30_claude_code_plugin_probe_effect import (
    runtime_c30_probe_tick as tick,
)
from omnimarket.nodes.node_c30_claude_code_plugin_probe_effect.handlers import (
    handler_c30_claude_code_plugin_probe as handler_module,
)

pytestmark = pytest.mark.unit


def _wire(tmp: Path, monkeypatch: pytest.MonkeyPatch, *, event_bus: bool) -> None:
    doc: dict[str, object] = {
        "default_profile": "inmemory",
        "profiles": {"inmemory": {"kind": "inmemory"}},
        "history_file": "runs.jsonl",
        "history_keep": 2,
        "result_topic": handler_module.contract_topics()[0],
    }
    if event_bus:
        doc["event_bus"] = {"type": "kafka", "lane": "dev"}
    path = tmp / "overlay.yaml"
    path.write_text(yaml.safe_dump(doc))
    monkeypatch.setenv(handler_module.OVERLAY_FILE_ENV, str(path))
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
        ["--omni-home", str(tmp_path), "--state-dir", str(tmp_path / "state")]
    )
    assert code != tick.EXIT_NOT_PUBLISHED
    assert len(sent) == 1
    topic, value, key = sent[0]
    assert topic.endswith(handler_module.contract_topics()[0])
    record = json.loads(value)
    assert record["criterion"] == "C30"
    assert "payload" not in record
    assert key == b"C30"
    assert asked == [tick.NODE]


def test_a_tick_whose_overlay_declares_no_bus_exits_not_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _wire(tmp_path, monkeypatch, event_bus=False)
    monkeypatch.setenv("OMNI_HOME", str(tmp_path))
    monkeypatch.setenv("ONEX_WORKSPACE_CONFIG_ROOT", str(tmp_path))
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path / "no-roots"))
    code = tick.main(
        ["--omni-home", str(tmp_path), "--state-dir", str(tmp_path / "state")]
    )
    assert code == tick.EXIT_NOT_PUBLISHED
