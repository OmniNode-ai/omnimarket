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
    _served_models_cache,
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


def _clear_secret_store_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate secret resolution: no lane config path + fresh convention store.

    OMN-13861 auth tests resolve ``secret_ref`` through the convention-fallback
    default store (``llm.x.api_key`` → ``LLM_X_API_KEY``). Clear any
    ``ONEX_SECRET_RESOLVER_CONFIG_PATH`` a sibling test set and drop the cached
    ``_configured_secret_store`` so the default (convention) store is used.
    """
    from omnimarket.inference.secret_store_resolver import (
        clear_secret_store_resolver_cache,
    )

    monkeypatch.delenv("ONEX_SECRET_RESOLVER_CONFIG_PATH", raising=False)
    clear_secret_store_resolver_cache()


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


@pytest.fixture(autouse=True)
def _no_served_models_evidence_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """OMN-16419: default the served-models guard to "no evidence" (None).

    Without this, every test in this module that doesn't care about the
    guard would make a REAL network GET to ``<endpoint>/v1/models`` (some of
    which are live internet hosts, e.g. the Gemini endpoint in
    ``test_complete_chat_completions_endpoint_is_not_double_appended``) —
    slow, non-deterministic, and out of scope for those tests. ``None`` means
    "no evidence either way", which is the guard's own documented no-op case
    (unchanged pre-ticket behavior). Tests that exercise the guard itself
    override this within their own body via a second ``monkeypatch.setattr``.
    """
    _served_models_cache.clear()
    monkeypatch.setattr(
        transport, "probe_served_models", lambda *_args, **_kwargs: None
    )


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
    def test_resolved_topic_constants_match_documented_literal_values(self) -> None:
        """Lock every contract-declared publish topic to its literal wire string.

        A regression test, not a gate-satisfaction placeholder: the topic
        constants are resolved dynamically from ``contract.yaml`` at import
        time (``_single_contract_topic``); this pins the resolved value to
        the documented canonical string so a silent contract rename is caught
        here rather than only downstream at a live boundary.
        """
        assert (
            TOPIC_DELEGATION_CALL_COMPLETED
            == "onex.evt.omnimarket.delegation-call-completed.v1"
        )
        assert (
            TOPIC_DELEGATION_ESCALATION_TRIGGERED
            == "onex.evt.omnimarket.delegation-escalation-triggered.v1"
        )
        assert (
            TOPIC_DELEGATION_ALL_TIERS_FAILED
            == "onex.evt.omnimarket.delegation-all-tiers-failed.v1"
        )

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
    def test_secret_ref_resolves_to_authorization_bearer_header(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OMN-13861: a request carrying ``secret_ref`` attaches ``Authorization``.

        The routing authority carries only the logical ``secret_ref``; the effect
        handler resolves it to the literal key at the call boundary and posts an
        ``Authorization: Bearer <key>`` header alongside the static bifrost headers.
        Without this, every authenticated cloud tier 400'd on the bus-less path.
        """
        _clear_secret_store_cache(monkeypatch)
        monkeypatch.setenv("LLM_TESTPROVIDER_API_KEY", "sk-test-abc123")
        api_resp = _make_api_response("ok", tokens_in=1, tokens_out=1)
        captured: dict[str, Any] = {}

        def fake_post(
            *,
            endpoint_url: str,
            payload: dict[str, Any],
            timeout_seconds: float,
            extra_headers: dict[str, str] | None = None,
            runtime_profile: str | None = None,
        ) -> transport.ModelTransportResponse:
            captured["extra_headers"] = extra_headers
            return transport.ModelTransportResponse(
                status_code=200, json_body=api_resp, latency_ms=3
            )

        monkeypatch.setattr(transport, "post_chat_completion", fake_post)
        with patch(f"{_HANDLER_MODULE}._is_endpoint_healthy", return_value=True):
            result = HandlerLlmDelegationCall()(
                _make_request(
                    secret_ref="llm.testprovider.api_key",
                    extra_headers={"X-Title": "OmniNode ONEX"},
                )
            )

        assert result.success is True
        headers = captured["extra_headers"]
        assert headers["Authorization"] == "Bearer sk-test-abc123"
        # The static bifrost headers are preserved alongside the resolved credential.
        assert headers["X-Title"] == "OmniNode ONEX"

    @pytest.mark.unit
    def test_none_secret_ref_sends_no_authorization_header(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unauthenticated local backend (secret_ref=None) adds no Authorization."""
        _clear_secret_store_cache(monkeypatch)
        api_resp = _make_api_response("ok", tokens_in=1, tokens_out=1)
        captured: dict[str, Any] = {}

        def fake_post(
            *,
            endpoint_url: str,
            payload: dict[str, Any],
            timeout_seconds: float,
            extra_headers: dict[str, str] | None = None,
            runtime_profile: str | None = None,
        ) -> transport.ModelTransportResponse:
            captured["extra_headers"] = extra_headers
            return transport.ModelTransportResponse(
                status_code=200, json_body=api_resp, latency_ms=3
            )

        monkeypatch.setattr(transport, "post_chat_completion", fake_post)
        with patch(f"{_HANDLER_MODULE}._is_endpoint_healthy", return_value=True):
            result = HandlerLlmDelegationCall()(_make_request(secret_ref=None))

        assert result.success is True
        assert "Authorization" not in captured["extra_headers"]

    @pytest.mark.unit
    def test_declared_but_unresolvable_secret_ref_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A declared secret_ref with no resolvable value fails closed.

        The handler must NOT make an unauthenticated call that would silently 400;
        the ``SecretResolutionError`` is caught as a transport failure so the port
        records a real failure rather than a misleading success.
        """
        _clear_secret_store_cache(monkeypatch)
        monkeypatch.delenv("LLM_ABSENTPROVIDER_API_KEY", raising=False)
        # transport.post must NOT be reached; make it explode if it is.
        _patch_post(monkeypatch, side_effect=AssertionError("must not POST unauthed"))

        with patch(f"{_HANDLER_MODULE}._is_endpoint_healthy", return_value=True):
            result = HandlerLlmDelegationCall()(
                _make_request(secret_ref="llm.absentprovider.api_key")
            )

        assert result.success is False
        assert result.failure_class == EnumDelegationFailureClass.UNKNOWN
        assert "absentprovider" in result.error_message.lower()

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


class TestModelAttributionMismatchGuard:
    """OMN-16419: the fail-closed model-attribution guard.

    Reproduces the exact defect this ticket fixes: an OpenAI-compat server
    (SGLang, live-observed at .201:8000) accepts ANY ``model`` string in a
    chat-completion request and echoes it back verbatim at HTTP 200 — served
    by whatever model is actually loaded. The response body's ``model`` field
    is therefore not trustworthy evidence of what actually served the
    request; only a live ``GET /v1/models`` read is. These tests stub that
    read (``transport.probe_served_models``) directly, never the
    chat-completions response, to prove the guard consults the right source.
    """

    def setup_method(self) -> None:
        _health_cache.clear()
        _served_models_cache.clear()

    @pytest.mark.unit
    def test_configured_model_absent_from_served_ids_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A stale configured model_name never reaches the network.

        Stubs ``transport.post_chat_completion`` to explode if called at all —
        the guard must reject BEFORE any chat-completion POST, since an
        SGLang-style server would return HTTP 200 for the stale name anyway
        (that HTTP 200 is exactly the silent-fail-open defect being closed).
        """
        monkeypatch.setattr(
            transport,
            "probe_served_models",
            lambda *_args, **_kwargs: frozenset({"qwen3.8"}),
        )
        _patch_post(
            monkeypatch,
            side_effect=AssertionError(
                "chat-completion POST must not be reached on a served-model mismatch"
            ),
        )

        with patch(f"{_HANDLER_MODULE}._is_endpoint_healthy", return_value=True):
            handler = HandlerLlmDelegationCall()
            result = handler(_make_request(model_id="Qwen3.6-35B-A3B"))

        assert result.success is False
        assert (
            result.failure_class
            == EnumDelegationFailureClass.MODEL_ATTRIBUTION_MISMATCH
        )
        assert result.error_message is not None
        assert "Qwen3.6-35B-A3B" in result.error_message
        assert "qwen3.8" in result.error_message
        assert result.served_model_id is None

    @pytest.mark.unit
    def test_configured_model_matches_served_ids_records_served_model_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A configured name confirmed by GET /v1/models is recorded as such."""
        monkeypatch.setattr(
            transport,
            "probe_served_models",
            lambda *_args, **_kwargs: frozenset({"qwen3.8"}),
        )
        api_resp = _make_api_response("ok", tokens_in=3, tokens_out=2)
        _patch_post(monkeypatch, json_body=api_resp)

        with patch(f"{_HANDLER_MODULE}._is_endpoint_healthy", return_value=True):
            handler = HandlerLlmDelegationCall()
            result = handler(_make_request(model_id="qwen3.8"))

        assert result.success is True
        assert result.served_model_id == "qwen3.8"

    @pytest.mark.unit
    def test_no_served_models_evidence_is_a_guard_no_op(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No /v1/models evidence (e.g. a cloud backend) must not block the call.

        This is the bounded-scope case: most non-local backends do not expose
        an OpenAI-compat model list at ``scheme://netloc/v1/models`` (their
        real surface lives at a provider-specific path), so the guard must be
        a no-op rather than fail-closed on the absence of evidence — only a
        confirmed MISMATCH fails closed.
        """
        monkeypatch.setattr(
            transport, "probe_served_models", lambda *_args, **_kwargs: None
        )
        api_resp = _make_api_response("ok", tokens_in=3, tokens_out=2)
        _patch_post(monkeypatch, json_body=api_resp)

        with patch(f"{_HANDLER_MODULE}._is_endpoint_healthy", return_value=True):
            handler = HandlerLlmDelegationCall()
            result = handler(_make_request(model_id="gemini-2.5-flash"))

        assert result.success is True
        assert result.served_model_id is None

    @pytest.mark.unit
    def test_mismatch_event_attribution_carries_server_confirmed_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OMN-16419 adjusts OMN-8022: prefer the server-confirmed id over the
        configured name in the emitted ``ModelLlmDelegationCompletedEvent``
        when the guard has live-confirmed it, so the ``llm-call-completed``
        event never carries a name the endpoint disputes."""
        monkeypatch.setattr(
            transport,
            "probe_served_models",
            lambda *_args, **_kwargs: frozenset({"qwen3.8"}),
        )
        api_resp = _make_api_response("ok", tokens_in=3, tokens_out=2)
        _patch_post(monkeypatch, json_body=api_resp)
        publisher = MagicMock()  # transport-mock-ok: event_publisher typed Any

        with patch(f"{_HANDLER_MODULE}._is_endpoint_healthy", return_value=True):
            handler = HandlerLlmDelegationCall()
            handler(_make_request(model_id="qwen3.8"), event_publisher=publisher)

        publisher.publish.assert_called_once()
        _topic, event = publisher.publish.call_args[0]
        assert event.model_id == "qwen3.8"
        assert event.selected_model == "qwen3.8"

    @pytest.mark.unit
    def test_handle_mismatch_returns_plain_failure_result_no_event(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Via the runtime ``handle()`` entrypoint: a mismatch returns the
        plain failure result (mirrors timeout/http-error/invalid-json) — it
        must NOT publish a ``ModelLlmDelegationCompletedEvent`` carrying the
        unserved name."""
        monkeypatch.setattr(
            transport,
            "probe_served_models",
            lambda *_args, **_kwargs: frozenset({"qwen3.8"}),
        )
        _patch_post(
            monkeypatch,
            side_effect=AssertionError("must not POST on a served-model mismatch"),
        )

        with patch(f"{_HANDLER_MODULE}._is_endpoint_healthy", return_value=True):
            handler = HandlerLlmDelegationCall()
            result = handler.handle(_make_request(model_id="Qwen3.6-35B-A3B"))

        assert isinstance(result, ModelLlmDelegationCallResult)
        assert result.success is False
        assert (
            result.failure_class
            == EnumDelegationFailureClass.MODEL_ATTRIBUTION_MISMATCH
        )
