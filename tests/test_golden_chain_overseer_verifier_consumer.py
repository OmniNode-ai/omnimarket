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

import json

import pytest

from omnimarket.nodes.node_overseer_verifier.handlers.handler_overseer_verifier_consumer import (
    TOPIC_PUBLISH,
    TOPIC_SUBSCRIBE,
    HandlerOverseerVerifierConsumer,
)


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
    consumer = HandlerOverseerVerifierConsumer()
    result = json.loads(consumer.process(_make_cmd()))

    assert result["passed"] is True
    assert result["verdict"] == "PASS"
    assert result["correlation_id"] == "corr-1234"
    assert result["failed_criteria"] == []
    assert "checks" in result
    assert len(result["checks"]) == 5  # all five check dimensions


# ---------------------------------------------------------------------------
# FAIL path — missing required field
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_consumer_fail_path_empty_task_id() -> None:
    """Empty task_id results in a FAIL completion event."""
    consumer = HandlerOverseerVerifierConsumer()
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
    consumer = HandlerOverseerVerifierConsumer()
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
    consumer = HandlerOverseerVerifierConsumer()
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
    consumer = HandlerOverseerVerifierConsumer()
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
    consumer = HandlerOverseerVerifierConsumer()
    result = json.loads(consumer.process(_make_cmd()))

    assert "timestamp" in result
    assert result["timestamp"]  # non-empty
