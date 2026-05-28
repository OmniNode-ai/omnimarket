# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Unit tests for HandlerDelegationAbRunner.

All LLM calls are mocked — no network, no API keys required.
"""

from __future__ import annotations

import hashlib

import pytest

from omnimarket.nodes.node_delegation_ab_runner.handlers.handler_delegation_ab_runner import (
    HandlerDelegationAbRunner,
    _estimate_cost,
    _evaluate_quality,
)
from omnimarket.nodes.node_delegation_ab_runner.models.model_ab_comparison_result import (
    ModelABComparisonResult,
)
from omnimarket.nodes.node_delegation_ab_runner.models.model_delegation_ab_request import (
    ModelDelegationAbRequest,
    ModelDelegationPathConfig,
)
from omnimarket.nodes.node_delegation_ab_runner.models.model_delegation_path_result import (
    ModelDelegationPathResult,
)

# ── fixtures ──────────────────────────────────────────────────────────────────

_BASELINE_CFG = ModelDelegationPathConfig(
    label="baseline",
    endpoint_url="http://frontier.example.com",
    model_id="gemini-2.5-flash",
    api_key="test-key",
    protocol="openai_compatible",
    timeout_seconds=30.0,
    is_delegated=False,
)

_DELEGATED_CFG = ModelDelegationPathConfig(
    label="delegated",
    endpoint_url="http://local.example.com",
    model_id="qwen3-coder-30b",
    api_key="",
    protocol="openai_compatible",
    timeout_seconds=30.0,
    is_delegated=True,
)


def _make_request(
    quality_threshold: float = 0.0,
    correlation_id: str = "test-corr-001",
) -> ModelDelegationAbRequest:
    return ModelDelegationAbRequest(
        task_payload="Generate a PR description for a refactoring change.",
        system_prompt="You are a helpful assistant.",
        correlation_id=correlation_id,
        baseline=_BASELINE_CFG,
        delegated=_DELEGATED_CFG,
        quality_threshold=quality_threshold,
    )


def _make_llm_call(
    baseline_tokens: tuple[int, int] = (100, 200),
    delegated_tokens: tuple[int, int] = (80, 150),
    baseline_error: str = "",
    delegated_error: str = "",
    output: str = "This PR refactors the authentication module for clarity.",
) -> object:
    """Return a mock LLM call function that returns fixed values per path."""

    def _call(
        cfg: ModelDelegationPathConfig, task: str, sys: str, timeout: float
    ) -> tuple[int, int, str, str]:
        if cfg.is_delegated:
            if delegated_error:
                return 0, 0, "", delegated_error
            pt, ct = delegated_tokens
            return pt, ct, output, ""
        if baseline_error:
            return 0, 0, "", baseline_error
        pt, ct = baseline_tokens
        return pt, ct, output, ""

    return _call


# ── model unit tests ──────────────────────────────────────────────────────────


def test_estimate_cost_frontier() -> None:
    cost = _estimate_cost(1_000_000, 1_000_000, is_delegated=False)
    assert cost > 0.0
    assert cost > _estimate_cost(1_000_000, 1_000_000, is_delegated=True)


def test_estimate_cost_delegated_cheaper() -> None:
    frontier = _estimate_cost(500, 1000, is_delegated=False)
    delegated = _estimate_cost(500, 1000, is_delegated=True)
    assert delegated < frontier


def test_estimate_cost_zero_tokens() -> None:
    assert _estimate_cost(0, 0, is_delegated=False) == 0.0
    assert _estimate_cost(0, 0, is_delegated=True) == 0.0


def test_evaluate_quality_empty_output() -> None:
    assert _evaluate_quality("", 0.5) == 0.0


def test_evaluate_quality_short_output() -> None:
    assert _evaluate_quality("short", 0.5) == 0.0


def test_evaluate_quality_good_output() -> None:
    assert _evaluate_quality("This is a sufficiently long output string.", 0.5) == 1.0


def test_ab_comparison_result_compute_winner_delegated() -> None:
    baseline = ModelDelegationPathResult(
        label="baseline",
        model_id="gpt-4",
        endpoint_url="http://x",
        is_delegated=False,
        total_tokens=300,
        cost_usd=0.10,
        latency_ms=500,
        quality_passed=True,
    )
    delegated = ModelDelegationPathResult(
        label="delegated",
        model_id="qwen",
        endpoint_url="http://y",
        is_delegated=True,
        total_tokens=200,
        cost_usd=0.02,
        latency_ms=300,
        quality_passed=True,
    )
    result = ModelABComparisonResult.compute(
        correlation_id="c1",
        task_payload_hash="abc",
        baseline=baseline,
        delegated=delegated,
    )
    assert result.winner == "delegated"
    assert result.token_savings == 100
    assert result.cost_savings_usd == pytest.approx(0.08, abs=1e-6)
    assert result.latency_delta_ms == -200


def test_ab_comparison_result_compute_winner_baseline_on_error() -> None:
    baseline = ModelDelegationPathResult(
        label="baseline",
        model_id="gpt-4",
        endpoint_url="http://x",
        is_delegated=False,
        total_tokens=300,
        cost_usd=0.10,
        latency_ms=500,
        quality_passed=True,
    )
    delegated = ModelDelegationPathResult(
        label="delegated",
        model_id="qwen",
        endpoint_url="http://y",
        is_delegated=True,
        total_tokens=0,
        cost_usd=0.0,
        latency_ms=100,
        quality_passed=False,
        error="connection refused",
    )
    result = ModelABComparisonResult.compute(
        correlation_id="c2",
        task_payload_hash="abc",
        baseline=baseline,
        delegated=delegated,
    )
    assert result.winner == "baseline"


def test_ab_comparison_result_compute_winner_baseline_on_quality_fail() -> None:
    baseline = ModelDelegationPathResult(
        label="baseline",
        model_id="gpt-4",
        endpoint_url="http://x",
        is_delegated=False,
        total_tokens=300,
        cost_usd=0.10,
        latency_ms=500,
        quality_passed=True,
    )
    delegated = ModelDelegationPathResult(
        label="delegated",
        model_id="qwen",
        endpoint_url="http://y",
        is_delegated=True,
        total_tokens=200,
        cost_usd=0.02,
        latency_ms=300,
        quality_passed=False,  # quality gate failed
    )
    result = ModelABComparisonResult.compute(
        correlation_id="c3",
        task_payload_hash="abc",
        baseline=baseline,
        delegated=delegated,
    )
    assert result.winner == "baseline"
    assert result.delegated_quality_passed is False


# ── handler integration tests (mocked LLM) ───────────────────────────────────


def test_handler_both_paths_succeed() -> None:
    mock_call = _make_llm_call(
        baseline_tokens=(100, 200),
        delegated_tokens=(80, 150),
    )
    handler = HandlerDelegationAbRunner(llm_call=mock_call)  # type: ignore[arg-type]
    request = _make_request()
    result = handler.handle(request)

    assert isinstance(result, ModelABComparisonResult)
    assert result.correlation_id == "test-corr-001"
    assert result.baseline.total_tokens == 300
    assert result.delegated.total_tokens == 230
    assert result.token_savings == 70
    assert result.baseline.error == ""
    assert result.delegated.error == ""


def test_handler_task_payload_hash_is_stable() -> None:
    mock_call = _make_llm_call()
    handler = HandlerDelegationAbRunner(llm_call=mock_call)  # type: ignore[arg-type]
    request = _make_request()
    result = handler.handle(request)

    expected_hash = hashlib.sha256(request.task_payload.encode()).hexdigest()
    assert result.task_payload_hash == expected_hash


def test_handler_delegated_path_fails_baseline_wins() -> None:
    mock_call = _make_llm_call(
        delegated_error="connection timeout",
    )
    handler = HandlerDelegationAbRunner(llm_call=mock_call)  # type: ignore[arg-type]
    result = handler.handle(_make_request())

    assert result.delegated.error == "connection timeout"
    assert result.winner == "baseline"
    assert result.delegated.total_tokens == 0


def test_handler_quality_gate_pass() -> None:
    mock_call = _make_llm_call(
        delegated_tokens=(80, 150),
        output="This is a well-formed PR description with enough content.",
    )
    handler = HandlerDelegationAbRunner(llm_call=mock_call)  # type: ignore[arg-type]
    result = handler.handle(_make_request(quality_threshold=0.5))

    assert result.delegated.quality_passed is True
    assert result.winner == "delegated"


def test_handler_quality_gate_fail_short_output() -> None:
    mock_call = _make_llm_call(
        delegated_tokens=(10, 5),
        output="short",  # below quality threshold
    )
    handler = HandlerDelegationAbRunner(llm_call=mock_call)  # type: ignore[arg-type]
    result = handler.handle(_make_request(quality_threshold=0.5))

    assert result.delegated.quality_passed is False
    assert result.winner == "baseline"


def test_handler_cost_savings_computed() -> None:
    mock_call = _make_llm_call(
        baseline_tokens=(1000, 2000),
        delegated_tokens=(800, 1200),
    )
    handler = HandlerDelegationAbRunner(llm_call=mock_call)  # type: ignore[arg-type]
    result = handler.handle(_make_request())

    assert result.cost_savings_usd > 0.0
    assert result.baseline.cost_usd > result.delegated.cost_usd


def test_handler_no_quality_gate_delegated_always_passes() -> None:
    mock_call = _make_llm_call(
        delegated_tokens=(50, 50),
        output="x",  # would fail quality gate if threshold > 0
    )
    handler = HandlerDelegationAbRunner(llm_call=mock_call)  # type: ignore[arg-type]
    result = handler.handle(_make_request(quality_threshold=0.0))

    assert result.delegated.quality_passed is True


def test_handler_pricing_manifest_hash_propagated() -> None:
    mock_call = _make_llm_call()
    handler = HandlerDelegationAbRunner(llm_call=mock_call)  # type: ignore[arg-type]
    request = ModelDelegationAbRequest(
        task_payload="test",
        correlation_id="c-hash",
        baseline=_BASELINE_CFG,
        delegated=_DELEGATED_CFG,
        pricing_manifest_hash="abc123def456",
    )
    result = handler.handle(request)
    assert result.pricing_manifest_hash == "abc123def456"


def test_handler_both_paths_fail_baseline_wins() -> None:
    mock_call = _make_llm_call(
        baseline_error="frontier down",
        delegated_error="local down",
    )
    handler = HandlerDelegationAbRunner(llm_call=mock_call)  # type: ignore[arg-type]
    result = handler.handle(_make_request())

    assert result.baseline.error == "frontier down"
    assert result.delegated.error == "local down"
    assert result.winner == "baseline"


def test_handler_import() -> None:
    from omnimarket.nodes.node_delegation_ab_runner.handlers.handler_delegation_ab_runner import (
        HandlerDelegationAbRunner,
    )

    assert HandlerDelegationAbRunner is not None
