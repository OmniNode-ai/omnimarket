# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Unit tests for handler_llm_delegation_call (OMN-11776)."""

from __future__ import annotations

import time
from decimal import Decimal
from unittest.mock import MagicMock, patch

import httpx
import pytest

from omnimarket.enums.enum_cost_basis import EnumCostBasis
from omnimarket.enums.enum_delegation_failure_class import EnumDelegationFailureClass
from omnimarket.enums.enum_usage_source import EnumUsageSource
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_llm_delegation_call import (
    TOPIC_DELEGATION_ALL_TIERS_FAILED,
    TOPIC_DELEGATION_CALL_COMPLETED,
    TOPIC_DELEGATION_ESCALATION_TRIGGERED,
    HandlerLlmDelegationCall,
    _health_cache,
    _is_endpoint_healthy,
)
from omnimarket.nodes.node_llm_delegation_call_effect.models.model_llm_delegation_call_request import (
    ModelLlmDelegationCallRequest,
)


def _make_request(**overrides: object) -> ModelLlmDelegationCallRequest:
    defaults: dict[str, object] = {
        "request_id": "req-001",
        "correlation_id": "corr-001",
        "causation_id": "caus-001",
        "model_id": "Qwen/Qwen3-Coder-30B",
        "endpoint_ref": "LLM_LOCAL_PRIMARY_URL",
        "prompt": "Write a hello world function.",
        "prompt_hash": "abc123",
        "task_type": "codegen",
        "model_tier": "local",
        "provider": "vllm",
    }
    defaults.update(overrides)
    return ModelLlmDelegationCallRequest(**defaults)


def _make_api_response(
    content: str = "hello world", tokens_in: int = 10, tokens_out: int = 20
) -> dict:
    return {
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": tokens_in, "completion_tokens": tokens_out},
    }


class TestHealthProbeCache:
    def setup_method(self) -> None:
        _health_cache.clear()

    def test_healthy_endpoint_is_cached(self) -> None:
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_client.get.return_value = mock_resp

        result1 = _is_endpoint_healthy("http://localhost:8000", mock_client)
        result2 = _is_endpoint_healthy("http://localhost:8000", mock_client)

        assert result1 is True
        assert result2 is True
        assert mock_client.get.call_count == 1  # second call hits cache

    def test_unhealthy_endpoint_is_cached(self) -> None:
        mock_client = MagicMock()
        mock_client.get.side_effect = httpx.ConnectError("refused")

        result1 = _is_endpoint_healthy("http://localhost:9999", mock_client)
        result2 = _is_endpoint_healthy("http://localhost:9999", mock_client)

        assert result1 is False
        assert result2 is False
        assert mock_client.get.call_count == 1

    def test_cache_expires_after_ttl(self) -> None:
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_client.get.return_value = mock_resp

        _health_cache["http://example.com"] = (time.monotonic() - 61, True)
        _is_endpoint_healthy("http://example.com", mock_client)

        assert mock_client.get.call_count == 1  # cache expired, re-probed

    def test_500_response_marks_unhealthy(self) -> None:
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 503
        mock_client.get.return_value = mock_resp

        result = _is_endpoint_healthy("http://bad.endpoint", mock_client)
        assert result is False


class TestHandlerLlmDelegationCall:
    def setup_method(self) -> None:
        _health_cache.clear()

    @pytest.mark.unit
    def test_missing_endpoint_env_var_returns_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("LLM_LOCAL_PRIMARY_URL", raising=False)
        handler = HandlerLlmDelegationCall()
        request = _make_request()

        result = handler(request)

        assert result.success is False
        assert result.failure_class == EnumDelegationFailureClass.MODEL_UNAVAILABLE
        assert result.endpoint_healthy is False

    @pytest.mark.unit
    def test_unhealthy_endpoint_returns_failure_and_emits_all_tiers_failed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LLM_LOCAL_PRIMARY_URL", "http://localhost:8000")
        publisher = MagicMock()

        with patch(
            "omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_llm_delegation_call._is_endpoint_healthy",
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
        monkeypatch.setenv("LLM_LOCAL_PRIMARY_URL", "http://localhost:8000")
        publisher = MagicMock()
        api_resp = _make_api_response(
            "def hello(): return 'world'", tokens_in=50, tokens_out=30
        )

        mock_response = MagicMock()
        mock_response.json.return_value = api_resp
        mock_response.raise_for_status.return_value = None

        with (
            patch(
                "omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_llm_delegation_call._is_endpoint_healthy",
                return_value=True,
            ),
            patch("httpx.Client") as mock_client_cls,
        ):
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = mock_response
            mock_client_cls.return_value = mock_client

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
    def test_successful_call_emits_completed_event(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LLM_LOCAL_PRIMARY_URL", "http://localhost:8000")
        publisher = MagicMock()
        api_resp = _make_api_response("result text", tokens_in=10, tokens_out=5)

        mock_response = MagicMock()
        mock_response.json.return_value = api_resp
        mock_response.raise_for_status.return_value = None

        with (
            patch(
                "omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_llm_delegation_call._is_endpoint_healthy",
                return_value=True,
            ),
            patch("httpx.Client") as mock_client_cls,
        ):
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = mock_response
            mock_client_cls.return_value = mock_client

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
        monkeypatch.setenv("LLM_LOCAL_PRIMARY_URL", "http://localhost:8000")

        with (
            patch(
                "omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_llm_delegation_call._is_endpoint_healthy",
                return_value=True,
            ),
            patch("httpx.Client") as mock_client_cls,
        ):
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.side_effect = httpx.TimeoutException("timed out")
            mock_client_cls.return_value = mock_client

            handler = HandlerLlmDelegationCall()
            result = handler(_make_request())

        assert result.success is False
        assert result.failure_class == EnumDelegationFailureClass.TIMEOUT

    @pytest.mark.unit
    def test_rate_limit_returns_rate_limited_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LLM_LOCAL_PRIMARY_URL", "http://localhost:8000")

        mock_resp = MagicMock()
        mock_resp.status_code = 429
        http_error = httpx.HTTPStatusError(
            "429", request=MagicMock(), response=mock_resp
        )

        with (
            patch(
                "omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_llm_delegation_call._is_endpoint_healthy",
                return_value=True,
            ),
            patch("httpx.Client") as mock_client_cls,
        ):
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.side_effect = http_error
            mock_client_cls.return_value = mock_client

            handler = HandlerLlmDelegationCall()
            result = handler(_make_request())

        assert result.success is False
        assert result.failure_class == EnumDelegationFailureClass.RATE_LIMITED

    @pytest.mark.unit
    def test_empty_choices_returns_invalid_json_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LLM_LOCAL_PRIMARY_URL", "http://localhost:8000")

        mock_response = MagicMock()
        mock_response.json.return_value = {"choices": [], "usage": {}}
        mock_response.raise_for_status.return_value = None

        with (
            patch(
                "omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_llm_delegation_call._is_endpoint_healthy",
                return_value=True,
            ),
            patch("httpx.Client") as mock_client_cls,
        ):
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = mock_response
            mock_client_cls.return_value = mock_client

            handler = HandlerLlmDelegationCall()
            result = handler(_make_request())

        assert result.success is False
        assert result.failure_class == EnumDelegationFailureClass.INVALID_JSON

    @pytest.mark.unit
    def test_no_publisher_does_not_raise(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM_LOCAL_PRIMARY_URL", "http://localhost:8000")
        api_resp = _make_api_response("ok", tokens_in=5, tokens_out=5)

        mock_response = MagicMock()
        mock_response.json.return_value = api_resp
        mock_response.raise_for_status.return_value = None

        with (
            patch(
                "omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_llm_delegation_call._is_endpoint_healthy",
                return_value=True,
            ),
            patch("httpx.Client") as mock_client_cls,
        ):
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = mock_response
            mock_client_cls.return_value = mock_client

            handler = HandlerLlmDelegationCall()
            result = handler(_make_request(), event_publisher=None)

        assert result.success is True

    @pytest.mark.unit
    def test_emit_escalation_publishes_correct_topic(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        publisher = MagicMock()
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
        monkeypatch.setenv("LLM_LOCAL_PRIMARY_URL", "http://localhost:8000")
        api_resp = _make_api_response("content", tokens_in=1000, tokens_out=500)

        mock_response = MagicMock()
        mock_response.json.return_value = api_resp
        mock_response.raise_for_status.return_value = None

        with (
            patch(
                "omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_llm_delegation_call._is_endpoint_healthy",
                return_value=True,
            ),
            patch("httpx.Client") as mock_client_cls,
        ):
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = mock_response
            mock_client_cls.return_value = mock_client

            handler = HandlerLlmDelegationCall()
            result = handler(_make_request())

        assert result.actual_cost_usd > Decimal("0")
        assert result.opus_equivalent_cost_usd > result.actual_cost_usd
        assert result.savings_usd > Decimal("0")
