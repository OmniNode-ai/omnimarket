# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
# onex-allow-file OMN-10580 reason="test fixture — uses lab LLM endpoint in routing fixtures; not a runtime default"
"""Golden chain tests for HandlerLocalSupervisor.

Covers: executes routing decision, retries on verifier fail, escalates after budget exhausted.
All tests are pure Python — no I/O, no HTTP, no LLM calls.

Related:
    - OMN-8050: node_local_supervisor in omnimarket
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_local_supervisor.handlers.handler_local_supervisor import (
    TOPIC_COMPLETED,
    TOPIC_ESCALATED,
    TOPIC_SUBSCRIBE,
    HandlerLocalSupervisor,
)
from omnimarket.nodes.node_local_supervisor.models.model_local_supervisor_request import (
    EnumRetryStrategy,
    ModelLocalSupervisorRequest,
    ModelRoutingDecision,
)
from omnimarket.nodes.node_local_supervisor.models.model_local_supervisor_result import (
    EnumSupervisorVerdict,
)


def _make_decision(**overrides: object) -> ModelRoutingDecision:
    defaults: dict[str, object] = {
        "model_key": "qwen3-coder-30b",
        "endpoint_url": "http://192.168.86.201:8000",
        "role": "supervisor",
        "used_fallback": False,
    }
    defaults.update(overrides)
    return ModelRoutingDecision.model_validate(defaults)


def _make_request(**overrides: object) -> ModelLocalSupervisorRequest:
    defaults: dict[str, object] = {
        "routing_decision": _make_decision(),
        "prompt": "Generate a hello world function in Python.",
        "retry_budget": 2,
        "retry_strategy": EnumRetryStrategy.SAME_MODEL_SAME_CONTEXT,
        "correlation_id": "corr-test-1234",
    }
    defaults.update(overrides)
    return ModelLocalSupervisorRequest.model_validate(defaults)


# ---------------------------------------------------------------------------
# Topic constants
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_topic_constants_match_contract() -> None:
    """Topic constants must match node_local_supervisor/contract.yaml declarations."""
    assert TOPIC_SUBSCRIBE == "onex.cmd.omnimarket.local-supervisor-execute.v1"
    assert TOPIC_COMPLETED == "onex.evt.omnimarket.local-supervisor-completed.v1"
    assert TOPIC_ESCALATED == "onex.evt.omnimarket.local-supervisor-escalated.v1"


# ---------------------------------------------------------------------------
# test_local_supervisor_executes_routing_decision
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_local_supervisor_executes_routing_decision() -> None:
    """Supervisor invokes model via routing decision and returns PASS when verifier accepts."""
    invoker_calls: list[tuple[str, str, str]] = []

    def stub_invoker(endpoint_url: str, model_key: str, prompt: str) -> str:
        invoker_calls.append((endpoint_url, model_key, prompt))
        return "def hello(): return 'world'"

    handler = HandlerLocalSupervisor(model_invoker=stub_invoker)
    request = _make_request()
    result = handler.handle(request)

    assert result.verdict == EnumSupervisorVerdict.PASS
    assert result.escalated is False
    assert result.attempt_count == 1
    assert result.model_key == "qwen3-coder-30b"
    assert result.correlation_id == "corr-test-1234"
    assert "hello" in result.output

    # Verify it used the routing decision's endpoint and model_key
    assert len(invoker_calls) == 1
    endpoint_url, model_key, prompt = invoker_calls[0]
    assert endpoint_url == "http://192.168.86.201:8000"
    assert model_key == "qwen3-coder-30b"
    assert prompt == request.prompt


# ---------------------------------------------------------------------------
# test_local_supervisor_retries_on_verifier_fail
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_local_supervisor_retries_on_verifier_fail() -> None:
    """Supervisor retries when verifier rejects first output, passes on second attempt."""
    call_count = 0

    def stub_invoker(endpoint_url: str, model_key: str, prompt: str) -> str:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return "error: model overloaded"  # will fail verifier
        return "def solution(): return 42"

    verify_calls: list[str] = []

    def stub_verifier(output: str, prompt: str) -> bool:
        verify_calls.append(output)
        # Fail on first call (error response), pass on second
        return not output.startswith("error:")

    handler = HandlerLocalSupervisor(model_invoker=stub_invoker, verifier=stub_verifier)
    request = _make_request(retry_budget=2)
    result = handler.handle(request)

    assert result.verdict == EnumSupervisorVerdict.PASS
    assert result.escalated is False
    assert result.attempt_count == 2
    assert call_count == 2
    assert len(verify_calls) == 2


# ---------------------------------------------------------------------------
# test_local_supervisor_escalates_after_budget_exhausted
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_local_supervisor_escalates_after_budget_exhausted() -> None:
    """Supervisor escalates when all retry attempts fail verification."""

    def stub_invoker(endpoint_url: str, model_key: str, prompt: str) -> str:
        return "error: service unavailable"

    def always_fail_verifier(output: str, prompt: str) -> bool:
        return False

    handler = HandlerLocalSupervisor(
        model_invoker=stub_invoker, verifier=always_fail_verifier
    )
    # budget=2 means attempts 1 and 2; attempt 2 hits TWO_STRIKE_THRESHOLD (>=3 skipped,
    # but budget=2 exhausts first)
    request = _make_request(retry_budget=2)
    result = handler.handle(request)

    assert result.verdict == EnumSupervisorVerdict.ESCALATE
    assert result.escalated is True
    assert result.output == ""
    assert result.model_key == "qwen3-coder-30b"
    assert result.correlation_id == "corr-test-1234"


# ---------------------------------------------------------------------------
# Two-Strike protocol: budget >= 3 triggers escalation at threshold
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_two_strike_escalates_at_threshold() -> None:
    """Two-Strike protocol: attempt_count >= 3 escalates regardless of strategy."""
    invoke_count = 0

    def stub_invoker(endpoint_url: str, model_key: str, prompt: str) -> str:
        nonlocal invoke_count
        invoke_count += 1
        return "bad output"

    def always_fail_verifier(output: str, prompt: str) -> bool:
        return False

    handler = HandlerLocalSupervisor(
        model_invoker=stub_invoker, verifier=always_fail_verifier
    )
    # budget=5, but Two-Strike threshold=3 should trigger on attempt 3
    request = _make_request(
        retry_budget=5,
        retry_strategy=EnumRetryStrategy.SAME_MODEL_SAME_CONTEXT,
    )
    result = handler.handle(request)

    assert result.verdict == EnumSupervisorVerdict.ESCALATE
    assert result.escalated is True
    # Two-Strike fires at attempt 3 without invoking (threshold check is pre-invoke)
    assert result.attempt_count == 3
    # Only 2 actual invocations (attempts 1 and 2) before threshold check on attempt 3
    assert invoke_count == 2


# ---------------------------------------------------------------------------
# TIER_ESCALATION strategy escalates immediately on verifier fail
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_tier_escalation_strategy_escalates_immediately() -> None:
    """TIER_ESCALATION strategy skips retries and escalates on first verifier failure."""

    def stub_invoker(endpoint_url: str, model_key: str, prompt: str) -> str:
        return "weak output"

    def always_fail_verifier(output: str, prompt: str) -> bool:
        return False

    handler = HandlerLocalSupervisor(
        model_invoker=stub_invoker, verifier=always_fail_verifier
    )
    request = _make_request(
        retry_budget=5,
        retry_strategy=EnumRetryStrategy.TIER_ESCALATION,
    )
    result = handler.handle(request)

    assert result.verdict == EnumSupervisorVerdict.ESCALATE
    assert result.escalated is True
    assert result.attempt_count == 1


# ---------------------------------------------------------------------------
# Invocation exception retries without crashing
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_invocation_exception_is_retried() -> None:
    """Network/invocation exceptions count as failed attempts and are retried."""
    call_count = 0

    def flaky_invoker(endpoint_url: str, model_key: str, prompt: str) -> str:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("network timeout")
        return "def answer(): return 1"

    handler = HandlerLocalSupervisor(model_invoker=flaky_invoker)
    request = _make_request(retry_budget=2)
    result = handler.handle(request)

    assert result.verdict == EnumSupervisorVerdict.PASS
    assert call_count == 2
    assert result.attempt_count == 2


# ---------------------------------------------------------------------------
# correlation_id propagation
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_correlation_id_propagated() -> None:
    """correlation_id from the request is echoed in the result."""

    def stub_invoker(endpoint_url: str, model_key: str, prompt: str) -> str:
        return "valid output"

    handler = HandlerLocalSupervisor(model_invoker=stub_invoker)
    request = _make_request(correlation_id="unique-corr-abc-789")
    result = handler.handle(request)

    assert result.correlation_id == "unique-corr-abc-789"
