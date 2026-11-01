# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Every port-path gate decision is recorded and published as a gate record.

The in-process port decides in process. Its attempt record names the checks the
gate evaluated, skipped and failed and the rule that decided, keyed by
correlation id and attempt index, and the same record goes to the topic the
node's contract declares when the run has a bus.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from omnimarket.events.emit_effect_topic_publisher import EmitEffectTopicPublisher
from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillAttemptRecord,
)
from omnimarket.models.delegation.wire.model_quality_gate_record import (
    ModelQualityGateRecord,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    _attempt_records,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports import (
    port_local_delegation_dispatch as port_mod,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.quality_gate_record_recorder import (
    publish_quality_gate_record,
    quality_gate_result_topic,
)
from omnimarket.nodes.node_llm_delegation_call_effect import (
    ModelLlmDelegationCallRequest,
    ModelLlmDelegationCallResult,
)
from omnimarket.routing.delegation_backend_resolution import (
    ModelResolvedDelegationBackend,
)
from tests.bus_lane_guard import resolve_test_bus_broker

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("stub_provider_quota_reader")]

TOPIC = "onex.evt.omnibase-infra.quality-gate-result.v1"
ANSWER = "### ANSWER\n```python\ndef add(a, b):\n    return a + b\n```"
_BACKEND = ModelResolvedDelegationBackend(
    backend_id="local-coder",
    model_id="Qwen3.6-35B-A3B",
    endpoint_ref="https://local.example/v1/chat/completions",
    tier="local",
    max_tokens=4096,
    timeout_ms=30000,
)


class _Published(EmitEffectTopicPublisher):
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def publish(
        self,
        *,
        topic: str,
        event_type: str,
        payload: Mapping[str, object],
        event_id: str,
        correlation_id: str | None = None,
        partition_key: str | None = None,
    ) -> bool:
        self.events.append(
            {
                "topic": topic,
                "event_type": event_type,
                "payload": dict(payload),
                "event_id": event_id,
                "correlation_id": correlation_id,
            }
        )
        return True

    def gate_events(self) -> list[dict[str, object]]:
        return [e for e in self.events if e["topic"] == TOPIC]


class _NoBus(EmitEffectTopicPublisher):
    """The emit effect with no broker: delivery is refused, never raised."""

    def publish(self, **_kwargs: object) -> bool:
        return False


class _BrokenBus(EmitEffectTopicPublisher):
    """A publisher that raises; the recorder must not let it reach the run."""

    def publish(self, **_kwargs: object) -> bool:
        raise ConnectionError("no broker")


def _port(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    publisher: EmitEffectTopicPublisher,
) -> LocalDelegationDispatchPort:
    monkeypatch.setattr(
        port_mod, "resolve_delegation_backend", lambda *_a, **_k: _BACKEND
    )
    monkeypatch.setattr(port_mod, "next_eligible_tier", lambda *_a, **_k: None)
    monkeypatch.setattr(port_mod, "tier_for_backend", lambda _backend_id: "local")
    monkeypatch.setattr(
        port_mod, "resolve_task_class_max_escalations", lambda _task_type: 0
    )

    def _effect(
        request: ModelLlmDelegationCallRequest,
    ) -> ModelLlmDelegationCallResult:
        return ModelLlmDelegationCallResult(
            request_id=request.request_id,
            success=True,
            content=ANSWER,
            tokens_in=10,
            tokens_out=20,
            latency_ms=5,
            actual_cost_usd=Decimal("0"),
            savings_usd=Decimal("0"),
        )

    return LocalDelegationDispatchPort(
        effect_handler=_effect,
        evidence_db_path=tmp_path / "d.sqlite",
        effect_process_boundary=False,
        terminal_publisher=publisher,
    )


def _dispatch(
    port: LocalDelegationDispatchPort, correlation_id: UUID
) -> dict[str, object]:
    return asyncio.run(
        port.dispatch(
            prompt="add two ints",
            task_type="code_generation",
            correlation_id=correlation_id,
            max_tokens=256,
            source_file_path=None,
            source_session_id=None,
            wait=True,
            execution_timeout_seconds=240,
            terminal_delivery_margin_seconds=60,
            quality_contract_mode="extend_task_class",
            acceptance_criteria=(),
            tenant_id=None,
            named_tests=None,
        )
    )


def _attempt_gate_records(result: dict[str, object]) -> list[ModelQualityGateRecord]:
    return [
        attempt.gate_record
        for attempt in _attempt_records(result)
        if attempt.gate_record is not None
    ]


def test_port_gate_decision_recorded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A code attempt whose tests were not named records the check it skipped."""
    correlation_id = uuid4()
    result = _dispatch(_port(monkeypatch, tmp_path, _Published()), correlation_id)

    (record,) = _attempt_gate_records(result)
    assert record.correlation_id == correlation_id
    assert record.task_type == "code_generation"
    assert "passes_existing_tests" in record.checks_skipped
    assert record.checks_evaluated
    assert record.checks_failed == tuple(
        e.rule for e in record.checks_evaluated if not e.passed
    )
    assert record.deciding_rule


def test_port_gate_record_keyed_by_attempt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The key is correlation id plus attempt index; a rerun repeats it."""
    correlation_id = uuid4()
    first = _attempt_gate_records(
        _dispatch(_port(monkeypatch, tmp_path, _Published()), correlation_id)
    )
    rerun = _attempt_gate_records(
        _dispatch(_port(monkeypatch, tmp_path, _Published()), correlation_id)
    )

    assert [r.record_key for r in first] == [f"{correlation_id}:0"]
    assert [r.record_key for r in rerun] == [r.record_key for r in first]
    receipt_keys = {r.record_key: r for r in (*first, *rerun)}
    assert len(receipt_keys) == 1


def test_port_gate_record_published(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The published record is the attempt's record, on the declared topic."""
    published = _Published()
    correlation_id = uuid4()
    result = _dispatch(_port(monkeypatch, tmp_path, published), correlation_id)

    assert quality_gate_result_topic() == TOPIC
    (event,) = published.gate_events()
    assert event["event_type"] == "omnibase-infra.quality-gate-result"
    assert event["correlation_id"] == str(correlation_id)
    (record,) = _attempt_gate_records(result)
    assert ModelQualityGateRecord.model_validate(event["payload"]) == record
    assert event["event_id"] == f"delegate-skill-gate-record-{record.record_key}"


def test_port_gate_record_published_inmemory_bus_carries_same_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The in-memory emit path delivers the JSON the attempt record holds."""
    sent: list[tuple[str, bytes]] = []

    class _InMemoryPublisher(EmitEffectTopicPublisher):
        """What the emit effect does on the in-memory bus: the payload as JSON."""

        def publish(self, **kwargs: object) -> bool:
            sent.append((str(kwargs["topic"]), json.dumps(kwargs["payload"]).encode()))
            return True

    result = _dispatch(_port(monkeypatch, tmp_path, _InMemoryPublisher()), uuid4())
    ((topic, value),) = [(t, v) for t, v in sent if t == TOPIC]
    (record,) = _attempt_gate_records(result)
    assert topic == TOPIC
    assert ModelQualityGateRecord.model_validate(json.loads(value)) == record


@pytest.mark.kafka
def test_redpanda_overlay_returns_the_same_gate_record(
    tmp_path: Path,
) -> None:
    """Redpanda gives back the record that was published (lab, -m kafka)."""
    from confluent_kafka import Consumer, Producer

    bootstrap = resolve_test_bus_broker()
    record = ModelQualityGateRecord(
        record_key=f"{uuid4()}:0",
        correlation_id=uuid4(),
        attempt_index=0,
        task_type="code_generation",
        passed=True,
        fail_category="pass",
        quality_score=1.0,
        checks_skipped=("passes_existing_tests",),
        deciding_rule="accepted",
    )
    producer = Producer({"bootstrap.servers": bootstrap})
    sent: list[object] = []
    producer.produce(
        TOPIC,
        key=str(record.correlation_id).encode(),
        value=json.dumps(record.model_dump(mode="json")).encode(),
        on_delivery=lambda err, _msg: sent.append(err),
    )
    producer.flush(10)
    assert sent == [None]
    consumer = Consumer(
        {
            "bootstrap.servers": bootstrap,
            "group.id": f"gate-record-{uuid4()}",
            "auto.offset.reset": "earliest",
        }
    )
    consumer.subscribe([TOPIC])
    try:
        found = None
        for _ in range(60):
            message = consumer.poll(1.0)
            if message is None or message.error():
                continue
            if message.key() == str(record.correlation_id).encode():
                found = ModelQualityGateRecord.model_validate_json(
                    message.value() or b""
                )
                break
    finally:
        consumer.close()
    assert found == record


def test_port_gate_record_without_bus(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No bus: the run completes and the record rides the attempt record only."""
    result = _dispatch(_port(monkeypatch, tmp_path, _NoBus()), uuid4())

    assert result["status"] == "completed"
    assert len(_attempt_gate_records(result)) == 1
    record = _attempt_gate_records(result)[0]
    assert publish_quality_gate_record(None, record) is False
    assert publish_quality_gate_record(_NoBus(), record) is False
    assert publish_quality_gate_record(_BrokenBus(), record) is False


def test_attempt_record_without_gate_record_still_parses() -> None:
    """An older recording carries no gate_record; it parses and re-dumps without one."""
    old = {
        "tier": "local",
        "backend_id": "local-coder",
        "model_id": "m",
        "quality_gate_passed": True,
    }
    record = ModelDelegateSkillAttemptRecord.model_validate(old)
    assert record.gate_record is None
    assert "gate_record" not in record.model_dump(mode="json")
