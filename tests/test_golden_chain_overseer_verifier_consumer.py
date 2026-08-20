# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain tests for HandlerOverseerVerifierConsumer.

Covers: PASS, FAIL, ESCALATE, malformed payload, missing correlation_id.
All tests are pure Python — no I/O, no Kafka, no LLM.

Related:
    - OMN-8031: node_overseer_verifier in omnimarket
    - OMN-8025: Overseer seam integration epic
"""

from __future__ import annotations

import asyncio
import json
from typing import cast

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_core.protocols.event_bus.protocol_event_bus_publisher import (
    ProtocolEventBusPublisher,
)

from omnimarket.nodes.node_overseer_verifier.handlers.handler_overseer_verifier_consumer import (
    TOPIC_PUBLISH,
    TOPIC_SUBSCRIBE,
    TOPIC_VERIFICATION_RECEIPT_START,
    HandlerOverseerVerifierConsumer,
)

_BUS = cast(ProtocolEventBusPublisher, EventBusInmemory())


def _make_cmd(**overrides: object) -> bytes:
    """Build a valid verify-command payload with sensible defaults."""
    defaults: dict[str, object] = {
        "correlation_id": "corr-1234",
        "task_id": "OMN-9999",
        "status": "running",
        "domain": "build_loop",
        "node_id": "node_build_loop_orchestrator",
        "attempt": 1,
        "confidence": 0.9,
        "cost_so_far": 0.01,
        "allowed_actions": ["dispatch", "complete"],
        "schema_version": "1.0",
    }
    defaults.update(overrides)
    return json.dumps(defaults).encode()


# ---------------------------------------------------------------------------
# Topic constants
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_topic_constants_match_contract() -> None:
    """Topic constants must match node_overseer_verifier/contract.yaml declarations."""
    assert TOPIC_SUBSCRIBE == "onex.cmd.omnimarket.overseer-verify.v1"
    assert TOPIC_PUBLISH == "onex.evt.omnimarket.overseer-verifier-completed.v1"


# ---------------------------------------------------------------------------
# PASS path
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_consumer_pass_path() -> None:
    """Valid complete request produces a PASS completion event."""
    consumer = HandlerOverseerVerifierConsumer(event_bus=_BUS)
    result = json.loads(consumer.process(_make_cmd()))

    assert result["passed"] is True
    assert result["verdict"] == "PASS"
    assert result["correlation_id"] == "corr-1234"
    assert result["failed_criteria"] == []
    assert "checks" in result
    assert len(result["checks"]) == 6  # all six check dimensions (incl. pr_checks_live)


# ---------------------------------------------------------------------------
# FAIL path — missing required field
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_consumer_fail_path_empty_task_id() -> None:
    """Empty task_id results in a FAIL completion event."""
    consumer = HandlerOverseerVerifierConsumer(event_bus=_BUS)
    result = json.loads(consumer.process(_make_cmd(task_id="")))

    assert result["passed"] is False
    assert result["verdict"] == "FAIL"
    assert result["correlation_id"] == "corr-1234"
    assert "input_completeness" in result["failed_criteria"]


# ---------------------------------------------------------------------------
# ESCALATE path — invariant violation
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_consumer_escalate_path_negative_cost() -> None:
    """Negative cost_so_far triggers ESCALATE (INVARIANT_VIOLATION)."""
    consumer = HandlerOverseerVerifierConsumer(event_bus=_BUS)
    result = json.loads(consumer.process(_make_cmd(cost_so_far=-5.0)))

    assert result["passed"] is False
    assert result["verdict"] == "ESCALATE"
    assert "invariant_preservation" in result["failed_criteria"]


# ---------------------------------------------------------------------------
# Malformed payload
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_consumer_malformed_payload_returns_fail() -> None:
    """Non-JSON bytes produce a FAIL response, not an exception."""
    consumer = HandlerOverseerVerifierConsumer(event_bus=_BUS)
    result = json.loads(consumer.process(b"not json at all {{{"))

    assert result["passed"] is False
    assert result["verdict"] == "FAIL"
    assert result["failure_class"] == "DATA_INTEGRITY"
    assert "consumer_error" in result["failed_criteria"]


# ---------------------------------------------------------------------------
# correlation_id propagation
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_consumer_propagates_correlation_id() -> None:
    """correlation_id from the command is echoed back in the response."""
    consumer = HandlerOverseerVerifierConsumer(event_bus=_BUS)
    payload = _make_cmd()
    data = json.loads(payload)
    data["correlation_id"] = "my-unique-corr-id"
    result = json.loads(consumer.process(json.dumps(data).encode()))

    assert result["correlation_id"] == "my-unique-corr-id"


# ---------------------------------------------------------------------------
# Timestamp present
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_consumer_response_has_timestamp() -> None:
    """Completion event always includes a timestamp field."""
    consumer = HandlerOverseerVerifierConsumer(event_bus=_BUS)
    result = json.loads(consumer.process(_make_cmd()))

    assert "timestamp" in result
    assert result["timestamp"]  # non-empty


# ---------------------------------------------------------------------------
# verification-receipt-start publish
# ---------------------------------------------------------------------------


class _RecordingPublisher:
    """Captures published (topic, value) pairs instead of reaching a broker."""

    def __init__(self) -> None:
        self.published: list[tuple[str, bytes]] = []

    async def publish(self, topic: str, key: bytes | None, value: bytes) -> None:
        self.published.append((topic, value))


@pytest.mark.unit
def test_verification_receipt_start_topic_matches_contract() -> None:
    """The third topic this consumer publishes, declared but previously untested."""
    assert (
        TOPIC_VERIFICATION_RECEIPT_START
        == "onex.cmd.omnimarket.verification-receipt-start.v1"
    )


@pytest.mark.unit
async def test_task_id_triggers_verification_receipt_start_publish() -> None:
    """A command carrying task_id must start a receipt alongside the verdict.

    This is what makes node_verification_receipt_generator produce formal
    evidence for a verification. The publish is fire-and-forget on the running
    loop, so the test is async — without a loop the handler logs a warning and
    silently skips, which is the failure mode worth pinning.
    """
    bus = _RecordingPublisher()
    consumer = HandlerOverseerVerifierConsumer(
        event_bus=cast(ProtocolEventBusPublisher, bus)
    )

    consumer.process(_make_cmd(task_id="OMN-1234", repo="omnimarket", pr_number=77))
    await asyncio.sleep(0)  # let the fire-and-forget publish task run

    receipt_starts = [
        json.loads(value)
        for topic, value in bus.published
        if topic == TOPIC_VERIFICATION_RECEIPT_START
    ]
    assert len(receipt_starts) == 1, (
        f"expected exactly one {TOPIC_VERIFICATION_RECEIPT_START} publish, "
        f"got topics {[t for t, _ in bus.published]}"
    )
    payload = receipt_starts[0]
    assert payload["task_id"] == "OMN-1234"
    assert payload["correlation_id"] == "corr-1234"
    assert payload["repo"] == "omnimarket"
    assert payload["pr_number"] == 77


@pytest.mark.unit
async def test_absent_task_id_publishes_no_receipt_start() -> None:
    """No task_id means no receipt to start — the publish must not fire blindly."""
    bus = _RecordingPublisher()
    consumer = HandlerOverseerVerifierConsumer(
        event_bus=cast(ProtocolEventBusPublisher, bus)
    )

    payload = json.loads(_make_cmd())
    del payload["task_id"]
    consumer.process(json.dumps(payload).encode())
    await asyncio.sleep(0)

    assert not [
        topic for topic, _ in bus.published if topic == TOPIC_VERIFICATION_RECEIPT_START
    ]
