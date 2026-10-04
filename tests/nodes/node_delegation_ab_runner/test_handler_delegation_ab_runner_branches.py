# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Boundary outcomes for the fixed baseline/delegated runner, with mocked HTTP."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_delegation_ab_runner.handlers.handler_delegation_ab_runner import (
    HandlerDelegationAbRunner,
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

_OUTPUT = "A complete response that clears the quality threshold."


def test_completed_output_topic_is_declared_and_externally_consumed() -> None:
    contract_path = (
        Path(__file__).resolve().parents[3]
        / "src"
        / "omnimarket"
        / "nodes"
        / "node_delegation_ab_runner"
        / "contract.yaml"
    )
    contract = yaml.safe_load(contract_path.read_text())
    publish_topics = contract["event_bus"]["publish_topics"]

    assert publish_topics == ["onex.evt.omnimarket.delegation-ab-run-completed.v1"]
    assert publish_topics == contract["externally_consumed_topics"]


def test_completed_output_topic_has_no_stale_state_coverage_baseline_entry() -> None:
    baseline_path = (
        Path(__file__).resolve().parents[3]
        / "scripts"
        / "validation"
        / "state_coverage_baseline.txt"
    )
    stale = (
        "node_delegation_ab_runner onex.evt.omnimarket.delegation-ab-run-completed.v1"
    )

    assert stale not in baseline_path.read_text().splitlines()


def _response() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "usage": {"prompt_tokens": 100, "completion_tokens": 200},
            "choices": [{"message": {"content": _OUTPUT}}],
        },
    )


def _handle_http(
    monkeypatch: pytest.MonkeyPatch,
    respond: Callable[[httpx.Request], httpx.Response],
    *,
    system_prompt: str = "",
    api_key: str = "",
    quality_threshold: float = 0.5,
) -> ModelABComparisonResult:
    real_client = httpx.Client

    def client(*, timeout: float) -> httpx.Client:
        return real_client(timeout=timeout, transport=httpx.MockTransport(respond))

    monkeypatch.setattr(httpx, "Client", client)
    request = ModelDelegationAbRequest(
        correlation_id="ab-boundary",
        task_payload="Compare the two paths.",
        system_prompt=system_prompt,
        baseline=ModelDelegationPathConfig(
            label="baseline",
            model_id="frontier-test",
            endpoint_url="https://baseline.invalid/",
            api_key=api_key,
        ),
        delegated=ModelDelegationPathConfig(
            label="delegated",
            model_id="delegated-test",
            endpoint_url="https://delegated.invalid/",
            is_delegated=True,
        ),
        quality_threshold=quality_threshold,
    )
    return HandlerDelegationAbRunner().handle(request)


def _path_result(**overrides: object) -> ModelDelegationPathResult:
    return ModelDelegationPathResult.model_validate(
        {
            "label": "baseline",
            "model_id": "test-model",
            "endpoint_url": "https://baseline.invalid",
            "is_delegated": False,
            "quality_score": 1.0,
            **overrides,
        }
    )


@pytest.mark.parametrize("system_prompt", ["", "Use the supplied facts."])
@pytest.mark.parametrize("api_key", ["", "test-key"])
def test_http_request_branches_delegated_winner(
    monkeypatch: pytest.MonkeyPatch, system_prompt: str, api_key: str
) -> None:
    calls: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return _response()

    result = _handle_http(
        monkeypatch, respond, system_prompt=system_prompt, api_key=api_key
    )

    assert result.winner == "delegated"
    assert len(calls) == 2
    assert [str(call.url) for call in calls] == [
        "https://baseline.invalid/v1/chat/completions",
        "https://delegated.invalid/v1/chat/completions",
    ]
    for call, model_id in zip(calls, ("frontier-test", "delegated-test"), strict=True):
        messages = [{"role": "user", "content": "Compare the two paths."}]
        if system_prompt:
            messages.insert(0, {"role": "system", "content": system_prompt})
        assert json.loads(call.content) == {
            "model": model_id,
            "messages": messages,
            "max_tokens": 2048,
        }
    assert calls[0].headers.get("authorization") == (
        f"Bearer {api_key}" if api_key else None
    )
    assert "authorization" not in calls[1].headers
    assert result.baseline.total_tokens == result.delegated.total_tokens == 300
    assert result.baseline.cost_usd == pytest.approx(0.0000675)
    assert result.delegated.cost_usd == pytest.approx(0.000003)


def test_empty_arm_list_rejected_without_required_paths() -> None:
    # This request schema has two required paths, rather than an arbitrary arm list.
    with pytest.raises(ValidationError) as exc_info:
        ModelDelegationAbRequest.model_validate(
            {"task_payload": "Compare", "correlation_id": "empty-arms", "arms": []}
        )

    assert {(error["loc"], error["type"]) for error in exc_info.value.errors()} == {
        (("baseline",), "missing"),
        (("delegated",), "missing"),
        (("arms",), "extra_forbidden"),
    }


@pytest.mark.parametrize(
    "choices",
    [[], [{}], [{"message": {}}]],
    ids=["empty-choice-list", "missing-message", "missing-content"],
)
def test_empty_arm_response_baseline_winner(
    monkeypatch: pytest.MonkeyPatch, choices: list[dict[str, object]]
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.host == "delegated.invalid":
            return httpx.Response(200, json={"choices": choices})
        return _response()

    result = _handle_http(monkeypatch, respond)

    assert result.winner == "baseline"
    assert result.baseline.raw_output == _OUTPUT
    assert result.delegated.raw_output == ""
    assert result.delegated.error == ""
    assert result.delegated.total_tokens == 0
    assert result.delegated.cost_usd == 0.0
    assert result.delegated.quality_score == 0.0
    assert result.delegated.quality_passed is False


@pytest.mark.parametrize("failed_path", ["baseline", "delegated"])
@pytest.mark.parametrize(
    "failure",
    [httpx.ConnectError, httpx.ReadTimeout],
    ids=["refused", "timeout"],
)
def test_one_arm_refused_or_timed_out_baseline_winner(
    monkeypatch: pytest.MonkeyPatch,
    failed_path: str,
    failure: type[httpx.RequestError],
) -> None:
    calls: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        calls.append(host)
        if host == f"{failed_path}.invalid":
            raise failure("arm unavailable", request=request)
        return _response()

    result = _handle_http(monkeypatch, respond)
    failed = getattr(result, failed_path)
    completed_path = "delegated" if failed_path == "baseline" else "baseline"
    completed = getattr(result, completed_path)

    # A failed baseline costs zero, so a completed nonzero-cost delegated path
    # still loses under the current cost comparison. Pin that asymmetry too.
    assert result.winner == "baseline"
    assert failed.error == "arm unavailable"
    assert failed.retry_count == 1
    assert failed.total_tokens == 0
    assert failed.cost_usd == 0.0
    assert completed.error == ""
    assert completed.raw_output == _OUTPUT
    assert completed.total_tokens == 300
    assert completed.retry_count == 0
    assert calls.count(f"{failed_path}.invalid") == 2
    assert calls.count(f"{completed_path}.invalid") == 1


def test_retry_recovery_delegated_winner(monkeypatch: pytest.MonkeyPatch) -> None:
    delegated_attempts = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal delegated_attempts
        if request.url.host == "delegated.invalid":
            delegated_attempts += 1
            if delegated_attempts == 1:
                raise httpx.ReadTimeout("first attempt timed out", request=request)
        return _response()

    result = _handle_http(monkeypatch, respond)

    assert result.winner == "delegated"
    assert delegated_attempts == 2
    assert result.delegated.retry_count == 1
    assert result.delegated.error == ""
    assert result.delegated.raw_output == _OUTPUT
    assert result.delegated.total_tokens == 300
    assert result.delegated.quality_passed is True


def test_http_error_baseline_winner(monkeypatch: pytest.MonkeyPatch) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.host == "delegated.invalid":
            return httpx.Response(503, json={"error": "temporarily unavailable"})
        return _response()

    result = _handle_http(monkeypatch, respond)

    assert result.winner == "baseline"
    assert "503" in result.delegated.error
    assert result.delegated.retry_count == 1
    assert result.delegated.total_tokens == 0
    assert result.baseline.raw_output == _OUTPUT


def test_indeterminate_both_arms_failed_uses_baseline_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("both arms unavailable", request=request)

    result = _handle_http(monkeypatch, respond)

    # No literal "indeterminate" outcome exists: both failures select baseline.
    assert result.winner == "baseline"
    for path in (result.baseline, result.delegated):
        assert path.error == "both arms unavailable"
        assert path.retry_count == 1
        assert path.raw_output == ""
        assert path.total_tokens == 0
        assert path.cost_usd == 0.0
    assert result.token_savings == 0
    assert result.cost_savings_usd == 0.0


def test_equal_score_and_cost_tie_selects_delegated_winner() -> None:
    baseline = _path_result(total_tokens=300, cost_usd=0.01)
    delegated = _path_result(
        label="delegated", is_delegated=True, total_tokens=300, cost_usd=0.01
    )
    result = ModelABComparisonResult.compute(
        "equal-score-tie", "payload-hash", baseline, delegated
    )

    assert baseline.quality_score == delegated.quality_score == 1.0
    assert result.winner == "delegated"
    assert result.token_savings == 0
    assert result.cost_savings_usd == 0.0


@pytest.mark.parametrize(
    ("usage", "expected_prompt", "expected_completion", "expected_cost"),
    [
        pytest.param(None, 0, 0, 0.0, id="missing-usage"),
        pytest.param({}, 0, 0, 0.0, id="empty-usage"),
        pytest.param(
            {"completion_tokens": 200}, 0, 200, 0.000002, id="missing-prompt-tokens"
        ),
        pytest.param(
            {"prompt_tokens": 100}, 100, 0, 0.000001, id="missing-completion-tokens"
        ),
    ],
)
def test_missing_token_usage_delegated_winner(
    monkeypatch: pytest.MonkeyPatch,
    usage: dict[str, int] | None,
    expected_prompt: int,
    expected_completion: int,
    expected_cost: float,
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.host == "baseline.invalid":
            return _response()
        data: dict[str, object] = {"choices": [{"message": {"content": _OUTPUT}}]}
        if usage is not None:
            data["usage"] = usage
        return httpx.Response(200, json=data)

    result = _handle_http(monkeypatch, respond)

    assert result.winner == "delegated"
    assert result.delegated.error == ""
    assert result.delegated.quality_passed is True
    assert result.delegated.prompt_tokens == expected_prompt
    assert result.delegated.completion_tokens == expected_completion
    assert result.delegated.total_tokens == expected_prompt + expected_completion
    assert result.delegated.cost_usd == pytest.approx(expected_cost)


@pytest.mark.parametrize("missing_field", ["cost_usd", "total_tokens"])
def test_missing_arm_result_field_defaults_delegated_winner(
    missing_field: str,
) -> None:
    fields: dict[str, object] = {
        "label": "delegated",
        "is_delegated": True,
        "total_tokens": 200,
        "cost_usd": 0.02,
    }
    del fields[missing_field]
    delegated = _path_result(**fields)
    result = ModelABComparisonResult.compute(
        "missing-result-field",
        "payload-hash",
        _path_result(total_tokens=300, cost_usd=0.1),
        delegated,
    )

    assert getattr(delegated, missing_field) == 0
    assert result.winner == "delegated"
    assert result.token_savings == (100 if missing_field == "cost_usd" else 300)
    assert result.cost_savings_usd == pytest.approx(
        0.1 if missing_field == "cost_usd" else 0.08
    )
