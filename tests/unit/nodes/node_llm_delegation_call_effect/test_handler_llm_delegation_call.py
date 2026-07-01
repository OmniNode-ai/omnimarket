# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Unit tests for handler_llm_delegation_call (OMN-11776, OMN-13160).

OMN-13160 moved the HTTP transport behind the runtime-profile-selected
``transport`` module (curl on ``local_macos_claude_hooks``, httpx elsewhere).
These tests patch the transport boundary (``transport.probe_health`` /
``transport.post_chat_completion``) rather than ``httpx.Client`` directly, so the
handler's failure classification and cost-telemetry logic is exercised
independent of which transport is selected.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
import yaml

from omnimarket.enums.enum_cost_basis import EnumCostBasis
from omnimarket.enums.enum_delegation_failure_class import EnumDelegationFailureClass
from omnimarket.enums.enum_usage_source import EnumUsageSource
from omnimarket.models.delegation.llm_cost_routing.model_llm_delegation_all_tiers_failed_event import (
    ModelLlmDelegationAllTiersFailedEvent,
)
from omnimarket.models.delegation.llm_cost_routing.model_llm_delegation_completed_event import (
    ModelLlmDelegationCompletedEvent,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers import transport
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_llm_delegation_call import (
    TOPIC_DELEGATION_ALL_TIERS_FAILED,
    TOPIC_DELEGATION_CALL_COMPLETED,
    TOPIC_DELEGATION_ESCALATION_TRIGGERED,
    TOPIC_DELEGATION_MODEL_DEGRADED,
    HandlerLlmDelegationCall,
    _health_cache,
    _is_endpoint_healthy,
)
from omnimarket.nodes.node_llm_delegation_call_effect.models.model_llm_delegation_call_request import (
    ModelLlmDelegationCallRequest,
)
from omnimarket.nodes.node_llm_delegation_call_effect.models.model_llm_delegation_call_result import (
    ModelLlmDelegationCallResult,
)

_HANDLER_MODULE = (
    "omnimarket.nodes.node_llm_delegation_call_effect.handlers."
    "handler_llm_delegation_call"
)


def _make_request(**overrides: object) -> ModelLlmDelegationCallRequest:
    defaults: dict[str, object] = {
        "request_id": "req-001",
        "correlation_id": "corr-001",
        "causation_id": "caus-001",
        "model_id": "Qwen/Qwen3-Coder-30B",
        "endpoint_ref": "http://localhost:8000",
        "prompt": "Write a hello world function.",
        "prompt_hash": "abc123",
        "task_type": "codegen",
        "model_tier": "local",
        "provider": "vllm",
        "timeout_seconds": 60.0,
    }
    defaults.update(overrides)
    return ModelLlmDelegationCallRequest(**defaults)


def _make_api_response(
    content: str = "hello world", tokens_in: int = 10, tokens_out: int = 20
) -> dict[str, Any]:
    return {
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": tokens_in, "completion_tokens": tokens_out},
    }


def _patch_post(
    monkeypatch: pytest.MonkeyPatch,
    *,
    json_body: dict[str, Any] | None = None,
    side_effect: BaseException | None = None,
) -> dict[str, Any]:
    """Patch transport.post_chat_completion to return a typed body or raise."""
    captured: dict[str, Any] = {}

    def fake_post(
        *,
        endpoint_url: str,
        payload: dict[str, Any],
        timeout_seconds: float,
        extra_headers: dict[str, str] | None = None,
        runtime_profile: str | None = None,
    ) -> transport.ModelTransportResponse:
        captured["endpoint_url"] = endpoint_url
        captured["payload"] = payload
        captured["timeout_seconds"] = timeout_seconds
        if side_effect is not None:
            raise side_effect
        return transport.ModelTransportResponse(
            status_code=200,
            json_body=json_body or {},
            latency_ms=7,
        )

    monkeypatch.setattr(transport, "post_chat_completion", fake_post)
    return captured


class TestHealthProbeCache:
    def setup_method(self) -> None:
        _health_cache.clear()

    def test_healthy_endpoint_is_cached(self, monkeypatch: pytest.MonkeyPatch) -> None:
        probe = MagicMock(return_value=True)
        monkeypatch.setattr(transport, "probe_health", probe)

        result1 = _is_endpoint_healthy("http://localhost:8000")
        result2 = _is_endpoint_healthy("http://localhost:8000")

        assert result1 is True
        assert result2 is True
        assert probe.call_count == 1  # second call hits cache

    def test_unhealthy_endpoint_is_cached(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        probe = MagicMock(return_value=False)
        monkeypatch.setattr(transport, "probe_health", probe)

        result1 = _is_endpoint_healthy("http://localhost:9999")
        result2 = _is_endpoint_healthy("http://localhost:9999")

        assert result1 is False
        assert result2 is False
        assert probe.call_count == 1

    def test_cache_expires_after_ttl(self, monkeypatch: pytest.MonkeyPatch) -> None:
        probe = MagicMock(return_value=True)
        monkeypatch.setattr(transport, "probe_health", probe)

        _health_cache["http://example.com"] = (time.monotonic() - 61, True)
        _is_endpoint_healthy("http://example.com")

        assert probe.call_count == 1  # cache expired, re-probed

    def test_500_response_marks_unhealthy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(transport, "probe_health", MagicMock(return_value=False))

        result = _is_endpoint_healthy("http://bad.endpoint")
        assert result is False


class TestHandlerLlmDelegationCall:
    def setup_method(self) -> None:
        _health_cache.clear()

    @pytest.mark.unit
    def test_delegation_publish_topics_are_resolved_by_suffix(self) -> None:
        contract_path = (
            Path(__file__).resolve().parents[4]
            / "src/omnimarket/nodes/node_llm_delegation_call_effect/contract.yaml"
        )
        with contract_path.open(encoding="utf-8") as fh:
            contract = yaml.safe_load(fh)

        publish_topics = contract["event_bus"]["publish_topics"]
        assert publish_topics[0].endswith("inference-response.v1")
        assert TOPIC_DELEGATION_CALL_COMPLETED in publish_topics
        assert TOPIC_DELEGATION_ESCALATION_TRIGGERED in publish_topics
        assert TOPIC_DELEGATION_ALL_TIERS_FAILED in publish_topics
        assert TOPIC_DELEGATION_MODEL_DEGRADED in publish_topics

    @pytest.mark.unit
    def test_invalid_endpoint_ref_returns_failure(self) -> None:
        handler = HandlerLlmDelegationCall()
        request = _make_request(endpoint_ref="LLM_LOCAL_PRIMARY_URL")

        result = handler(request)

        assert result.success is False
        assert result.failure_class == EnumDelegationFailureClass.MODEL_UNAVAILABLE
        assert result.endpoint_healthy is False

    @pytest.mark.unit
    def test_unhealthy_endpoint_returns_failure_and_emits_all_tiers_failed(
        self,
    ) -> None:
        publisher = MagicMock()  # transport-mock-ok: event_publisher typed Any

        with patch(
            f"{_HANDLER_MODULE}._is_endpoint_healthy",
            return_value=False,
        ):
            handler = HandlerLlmDelegationCall()
            result = handler(_make_request(), event_publisher=publisher)

        assert result.success is False
        assert result.endpoint_healthy is False
        assert result.failure_class == EnumDelegationFailureClass.MODEL_UNAVAILABLE
        publisher.publish.assert_called_once()
        topic = publisher.publish.call_args[0][0]
        assert topic == TOPIC_DELEGATION_ALL_TIERS_FAILED

    @pytest.mark.unit
    def test_successful_call_returns_result_with_cost_telemetry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        publisher = MagicMock()  # transport-mock-ok: event_publisher typed Any
        api_resp = _make_api_response(
            "def hello(): return 'world'", tokens_in=50, tokens_out=30
        )
        _patch_post(monkeypatch, json_body=api_resp)

        with patch(f"{_HANDLER_MODULE}._is_endpoint_healthy", return_value=True):
            handler = HandlerLlmDelegationCall()
            result = handler(_make_request(), event_publisher=publisher)

        assert result.success is True
        assert result.content == "def hello(): return 'world'"
        assert result.tokens_in == 50
        assert result.tokens_out == 30
        assert result.latency_ms >= 0
        assert result.actual_cost_usd >= Decimal("0")
        assert result.usage_source == EnumUsageSource.MEASURED
        assert result.cost_basis == EnumCostBasis.CLOUD_API_COST
        assert result.output_hash is not None

    @pytest.mark.unit
    def test_complete_chat_completions_endpoint_is_not_double_appended(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        api_resp = _make_api_response("ok", tokens_in=1, tokens_out=1)
        complete_endpoint = (
            "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
        )
        captured = _patch_post(monkeypatch, json_body=api_resp)

        with patch(f"{_HANDLER_MODULE}._is_endpoint_healthy", return_value=True):
            result = HandlerLlmDelegationCall()(
                _make_request(endpoint_ref=complete_endpoint)
            )

        assert result.success is True
        # The transport receives the COMPLETE endpoint URL verbatim — no append.
        assert captured["endpoint_url"] == complete_endpoint

    @pytest.mark.unit
    def test_request_timeout_seconds_is_threaded_to_transport(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OMN-13170: the request timeout flows to the transport, not a 120s cap.

        A 300s request timeout (the local-coder overlay's 300000ms) must reach
        post_chat_completion verbatim so large generations are not capped by the
        previously hardcoded transport default.
        """
        api_resp = _make_api_response("ok", tokens_in=1, tokens_out=1)
        captured = _patch_post(monkeypatch, json_body=api_resp)

        with patch(f"{_HANDLER_MODULE}._is_endpoint_healthy", return_value=True):
            result = HandlerLlmDelegationCall()(_make_request(timeout_seconds=300.0))

        assert result.success is True
        assert captured["timeout_seconds"] == 300.0

    @pytest.mark.unit
    def test_successful_call_emits_completed_event(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        publisher = MagicMock()  # transport-mock-ok: event_publisher typed Any
        api_resp = _make_api_response("result text", tokens_in=10, tokens_out=5)
        _patch_post(monkeypatch, json_body=api_resp)

        with patch(f"{_HANDLER_MODULE}._is_endpoint_healthy", return_value=True):
            handler = HandlerLlmDelegationCall()
            handler(_make_request(), event_publisher=publisher)

        publisher.publish.assert_called_once()
        topic, event = publisher.publish.call_args[0]
        assert topic == TOPIC_DELEGATION_CALL_COMPLETED
        assert event.success is True
        assert event.usage_source == EnumUsageSource.MEASURED
        assert event.prompt_hash == "abc123"

    @pytest.mark.unit
    def test_timeout_returns_timeout_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_post(monkeypatch, side_effect=httpx.TimeoutException("timed out"))

        with patch(f"{_HANDLER_MODULE}._is_endpoint_healthy", return_value=True):
            handler = HandlerLlmDelegationCall()
            result = handler(_make_request())

        assert result.success is False
        assert result.failure_class == EnumDelegationFailureClass.TIMEOUT

    @pytest.mark.unit
    def test_rate_limit_returns_rate_limited_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 429
        http_error = httpx.HTTPStatusError(
            "429", request=MagicMock(), response=mock_resp
        )
        _patch_post(monkeypatch, side_effect=http_error)

        with patch(f"{_HANDLER_MODULE}._is_endpoint_healthy", return_value=True):
            handler = HandlerLlmDelegationCall()
            result = handler(_make_request())

        assert result.success is False
        assert result.failure_class == EnumDelegationFailureClass.RATE_LIMITED

    @pytest.mark.unit
    def test_empty_choices_returns_invalid_json_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_post(monkeypatch, json_body={"choices": [], "usage": {}})

        with patch(f"{_HANDLER_MODULE}._is_endpoint_healthy", return_value=True):
            handler = HandlerLlmDelegationCall()
            result = handler(_make_request())

        assert result.success is False
        assert result.failure_class == EnumDelegationFailureClass.INVALID_JSON

    @pytest.mark.unit
    def test_no_publisher_does_not_raise(self, monkeypatch: pytest.MonkeyPatch) -> None:
        api_resp = _make_api_response("ok", tokens_in=5, tokens_out=5)
        _patch_post(monkeypatch, json_body=api_resp)

        with patch(f"{_HANDLER_MODULE}._is_endpoint_healthy", return_value=True):
            handler = HandlerLlmDelegationCall()
            result = handler(_make_request(), event_publisher=None)

        assert result.success is True

    @pytest.mark.unit
    def test_emit_escalation_publishes_correct_topic(self) -> None:
        publisher = MagicMock()  # transport-mock-ok: event_publisher typed Any
        handler = HandlerLlmDelegationCall()
        request = _make_request(attempt_number=2)

        handler.emit_escalation(
            request,
            failure_class=EnumDelegationFailureClass.QUALITY_GATE_FAILED,
            escalation_reason="score below threshold",
            next_model_id="claude-sonnet-4-6",
            event_publisher=publisher,
        )

        publisher.publish.assert_called_once()
        topic, event = publisher.publish.call_args[0]
        assert topic == TOPIC_DELEGATION_ESCALATION_TRIGGERED
        assert event.failure_class == EnumDelegationFailureClass.QUALITY_GATE_FAILED
        assert event.next_model_id == "claude-sonnet-4-6"
        assert event.attempt_number == 2

    @pytest.mark.unit
    def test_cost_calculation_nonzero_for_nonzero_tokens(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """cheap_cloud tier has nonzero pricing in registry → actual_cost_usd > 0."""
        api_resp = _make_api_response("content", tokens_in=1000, tokens_out=500)
        _patch_post(monkeypatch, json_body=api_resp)

        with patch(f"{_HANDLER_MODULE}._is_endpoint_healthy", return_value=True):
            handler = HandlerLlmDelegationCall()
            # cheap_cloud tier has cost_per_1k_tokens=0.002 in routing_tiers.yaml
            result = handler(_make_request(model_tier="cheap_cloud"))

        assert result.actual_cost_usd > Decimal("0")
        assert result.opus_equivalent_cost_usd > result.actual_cost_usd
        assert result.savings_usd > Decimal("0")

    @pytest.mark.unit
    def test_cost_calculation_uses_registry_price_for_local_tier(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Local tier has cost_per_1k_tokens=0.0 in registry → actual_cost is 0."""
        api_resp = _make_api_response("content", tokens_in=1000, tokens_out=500)
        _patch_post(monkeypatch, json_body=api_resp)

        with patch(f"{_HANDLER_MODULE}._is_endpoint_healthy", return_value=True):
            handler = HandlerLlmDelegationCall()
            result = handler(_make_request(model_tier="local"), event_publisher=None)

        assert result.success is True
        assert result.actual_cost_usd == Decimal("0")
        assert result.savings_usd > Decimal("0")

    @pytest.mark.unit
    def test_cost_calculation_falls_back_when_registry_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When registry YAML is unreadable, falls back to _FALLBACK_PRICE_PER_1M."""
        from omnimarket.nodes.node_llm_delegation_call_effect.handlers import (
            handler_llm_delegation_call as h,
        )

        api_resp = _make_api_response("content", tokens_in=1000, tokens_out=500)
        _patch_post(monkeypatch, json_body=api_resp)

        with (
            patch(f"{_HANDLER_MODULE}._is_endpoint_healthy", return_value=True),
            patch.object(h, "_get_tier_price_per_1m", return_value=None),
        ):
            handler = HandlerLlmDelegationCall()
            result = handler(
                _make_request(model_tier="unknown_tier"), event_publisher=None
            )

        assert result.success is True
        # With fallback default pricing, cost > 0 for nonzero tokens
        assert result.actual_cost_usd > Decimal("0")

    @pytest.mark.unit
    def test_terminal_followup_events_declared_external_consumed(self) -> None:
        contract_path = (
            Path(__file__).resolve().parents[4]
            / "src/omnimarket/nodes/node_llm_delegation_call_effect/contract.yaml"
        )
        with contract_path.open(encoding="utf-8") as fh:
            contract = yaml.safe_load(fh)

        externally_consumed = set(contract["externally_consumed_topics"])
        assert TOPIC_DELEGATION_ESCALATION_TRIGGERED in externally_consumed
        assert TOPIC_DELEGATION_ALL_TIERS_FAILED in externally_consumed
        assert "onex.evt.omnimarket.delegation-model-degraded.v1" in externally_consumed

    @pytest.mark.unit
    def test_generic_exception_returns_unknown_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A transport exception that is neither TimeoutException nor
        HTTPStatusError falls through to the catch-all ``except Exception``
        branch and is classified UNKNOWN (distinct from TIMEOUT/RATE_LIMITED/
        MODEL_UNAVAILABLE/INVALID_JSON)."""
        _patch_post(monkeypatch, side_effect=RuntimeError("unexpected transport error"))

        with patch(f"{_HANDLER_MODULE}._is_endpoint_healthy", return_value=True):
            handler = HandlerLlmDelegationCall()
            result = handler(_make_request())

        assert result.success is False
        assert result.failure_class == EnumDelegationFailureClass.UNKNOWN
        assert "unexpected transport error" in result.error_message

    @pytest.mark.unit
    def test_emit_model_degraded_publishes_correct_topic(self) -> None:
        publisher = MagicMock()  # transport-mock-ok: event_publisher typed Any
        handler = HandlerLlmDelegationCall()
        request = _make_request()
        window_start = datetime(2026, 5, 1, tzinfo=UTC)
        window_end = datetime(2026, 5, 1, 1, tzinfo=UTC)
        expires_at = datetime(2026, 5, 1, 2, tzinfo=UTC)

        handler.emit_model_degraded(
            request,
            window_start=window_start,
            window_end=window_end,
            attempt_count=10,
            escalation_count=6,
            threshold=0.5,
            expires_at=expires_at,
            reason="escalation rate exceeded threshold",
            event_publisher=publisher,
        )

        publisher.publish.assert_called_once()
        topic, event = publisher.publish.call_args[0]
        assert topic == TOPIC_DELEGATION_MODEL_DEGRADED
        assert event.model_id == request.model_id
        assert event.attempt_count == 10
        assert event.escalation_count == 6
        assert event.threshold == 0.5
        assert event.reason == "escalation rate exceeded threshold"


class TestHandlerHandleRuntimeEntrypoint:
    """Coverage for ``HandlerLlmDelegationCall.handle()`` — the ACTUAL
    contract-wired runtime dispatch entrypoint (handler_routing resolves
    ``handle``, never ``__call__``; see the module docstring on ``handle``).
    ``__call__`` above is the separate swarm/A2A synchronous path. Every
    boundary outcome ``handle()`` can return per its own docstring is
    exercised here: endpoint-resolution failure, unhealthy endpoint (returns
    the all-tiers-failed EVENT, not a result), success (returns the
    call-completed EVENT), and timeout/http-error/invalid-json (returns a
    plain failure ModelLlmDelegationCallResult, no event)."""

    def setup_method(self) -> None:
        _health_cache.clear()

    @pytest.mark.unit
    def test_handle_invalid_endpoint_ref_returns_failure_result(self) -> None:
        handler = HandlerLlmDelegationCall()
        request = _make_request(endpoint_ref="LLM_LOCAL_PRIMARY_URL")

        result = handler.handle(request)

        assert isinstance(result, ModelLlmDelegationCallResult)
        assert result.success is False
        assert result.failure_class == EnumDelegationFailureClass.MODEL_UNAVAILABLE
        assert result.endpoint_healthy is False

    @pytest.mark.unit
    def test_handle_unhealthy_endpoint_returns_all_tiers_failed_event(self) -> None:
        with patch(f"{_HANDLER_MODULE}._is_endpoint_healthy", return_value=False):
            handler = HandlerLlmDelegationCall()
            result = handler.handle(_make_request())

        assert isinstance(result, ModelLlmDelegationAllTiersFailedEvent)
        assert result.attempted_models == (_make_request().model_id,)
        assert result.failure_classes == (EnumDelegationFailureClass.MODEL_UNAVAILABLE,)

    @pytest.mark.unit
    def test_handle_successful_call_returns_completed_event(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        api_resp = _make_api_response("hello from handle()", tokens_in=8, tokens_out=4)
        _patch_post(monkeypatch, json_body=api_resp)

        with patch(f"{_HANDLER_MODULE}._is_endpoint_healthy", return_value=True):
            handler = HandlerLlmDelegationCall()
            result = handler.handle(_make_request())

        assert isinstance(result, ModelLlmDelegationCompletedEvent)
        assert result.success is True
        assert result.tokens_in == 8
        assert result.tokens_out == 4
        assert result.usage_source == EnumUsageSource.MEASURED

    @pytest.mark.unit
    def test_handle_timeout_returns_plain_failure_result_no_event(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_post(monkeypatch, side_effect=httpx.TimeoutException("timed out"))

        with patch(f"{_HANDLER_MODULE}._is_endpoint_healthy", return_value=True):
            handler = HandlerLlmDelegationCall()
            result = handler.handle(_make_request())

        # timeout/http-error/invalid-json return the plain result (no event) —
        # NOT ModelLlmDelegationCompletedEvent and NOT AllTiersFailedEvent.
        assert isinstance(result, ModelLlmDelegationCallResult)
        assert result.success is False
        assert result.failure_class == EnumDelegationFailureClass.TIMEOUT

    @pytest.mark.unit
    def test_handle_empty_choices_returns_plain_failure_result_no_event(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_post(monkeypatch, json_body={"choices": [], "usage": {}})

        with patch(f"{_HANDLER_MODULE}._is_endpoint_healthy", return_value=True):
            handler = HandlerLlmDelegationCall()
            result = handler.handle(_make_request())

        assert isinstance(result, ModelLlmDelegationCallResult)
        assert result.success is False
        assert result.failure_class == EnumDelegationFailureClass.INVALID_JSON
