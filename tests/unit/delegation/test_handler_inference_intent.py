# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for HandlerInferenceIntent (OMN-12294).

Verifies the contract-native inference handler that replaces the in-process
DelegationIntentBridge.handle_inference_intent() path.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import NAMESPACE_DNS, uuid4, uuid5

import httpx
import pytest
from omnibase_core.models.delegation.wire import (
    ModelDelegationRequest,
    ModelInferenceIntent,
    ModelInferenceResponseData,
)
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.protocols import ProtocolEventBusLike
from omnibase_infra.runtime.auto_wiring.handler_wiring import _make_dispatch_callback
from omnibase_infra.runtime.auto_wiring.models import ModelHandlerRef
from omnibase_infra.runtime.service_dispatch_result_applier import (
    DispatchResultApplier,
)

from omnimarket.nodes.node_delegation_orchestrator.enums import (
    EnumDelegationState,
)
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    HandlerDelegationWorkflow,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_event import (
    ModelDelegationEvent,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_result import (
    ModelDelegationResult,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_routing_decision import (
    ModelRoutingDecision,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_inference_intent import (
    TOPIC_INFERENCE_RESPONSE,
    HandlerInferenceIntent,
)


def _make_intent(**kwargs: object) -> ModelInferenceIntent:
    defaults: dict[str, object] = {
        "base_url": "http://localhost:8000/v1/chat/completions",
        "model": "test-model",
        "system_prompt": "You are a helpful assistant.",
        "prompt": "Write a test.",
        "max_tokens": 512,
        "temperature": 0.3,
        "timeout_seconds": 30.0,
        "correlation_id": uuid4(),
    }
    defaults.update(kwargs)
    return ModelInferenceIntent(**defaults)  # type: ignore[arg-type]


_SUCCESSFUL_HTTPX_RESPONSE = {
    "id": "chatcmpl-abc123",
    "choices": [{"message": {"content": "def test_foo(): pass"}}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
}


def _make_delegation_request(correlation_id: object) -> ModelDelegationRequest:
    return ModelDelegationRequest(
        prompt="Write unit tests for verify_registration.py",
        task_type="test",
        correlation_id=correlation_id,  # type: ignore[arg-type]
        max_tokens=512,
        emitted_at=datetime.now(UTC),
    )


def _make_terminal_routing_decision(correlation_id: object) -> ModelRoutingDecision:
    """Return a routing decision that cannot escalate after inference failure."""
    return ModelRoutingDecision(
        correlation_id=correlation_id,  # type: ignore[arg-type]
        task_type="test",
        selected_model="Qwen3.6-27B",
        selected_backend_id=uuid5(NAMESPACE_DNS, "omninode.ai/backends/qwen3.6-27b"),
        endpoint_url="http://192.168.86.201:8001",  # onex-allow-internal-ip OMN-12720 reason="live probe endpoint reproduced in failure-chain unit test"
        cost_tier="local",
        max_context_tokens=65536,
        max_tokens=65536,  # OMN-13345: contract backend output ceiling
        system_prompt="You are a test generation assistant.",
        rationale="Live probe selected the local Qwen endpoint.",
        tier_name="claude",
        timeout_ms=30000,
        dod_deterministic=("final_artifact_only",),
        dod_heuristic=("uses_pytest_mark_unit",),
    )


def _inference_event_model_ref() -> ModelHandlerRef:
    return ModelHandlerRef(
        name="ModelInferenceIntent",
        module="omnibase_core.models.delegation.wire",
    )


@pytest.mark.unit
class TestHandlerInferenceIntent:
    def test_success_returns_response_data(self) -> None:
        handler = HandlerInferenceIntent()
        intent = _make_intent()

        mock_response = MagicMock()
        mock_response.json.return_value = _SUCCESSFUL_HTTPX_RESPONSE
        mock_response.raise_for_status.return_value = None

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = mock_response
            mock_client_cls.return_value = mock_client

            result = handler.handle(intent)

        assert isinstance(result, ModelInferenceResponseData)
        assert result.correlation_id == intent.correlation_id
        assert result.content == "def test_foo(): pass"
        assert result.model_used == "test-model"
        assert result.prompt_tokens == 10
        assert result.completion_tokens == 20
        assert result.total_tokens == 30
        assert result.error_message == ""

    def test_posts_base_url_verbatim_no_path_append(self) -> None:
        """OMN-12815: the POST URL equals intent.base_url exactly — no append."""
        handler = HandlerInferenceIntent()
        complete_url = (
            "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
        )
        intent = _make_intent(base_url=complete_url)
        captured_urls: list[str] = []

        mock_response = MagicMock()
        mock_response.json.return_value = _SUCCESSFUL_HTTPX_RESPONSE
        mock_response.raise_for_status.return_value = None

        def _capture_post(url: str, **kwargs: object) -> MagicMock:
            captured_urls.append(url)
            return mock_response

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.side_effect = _capture_post
            mock_client_cls.return_value = mock_client

            handler.handle(intent)

        assert captured_urls == [complete_url]
        assert "/v1beta/openai/v1/chat/completions" not in captured_urls[0]

    def test_http_failure_returns_error_response(self) -> None:
        handler = HandlerInferenceIntent()
        intent = _make_intent()

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.side_effect = ConnectionRefusedError("refused")
            mock_client_cls.return_value = mock_client

            result = handler.handle(intent)

        assert result.correlation_id == intent.correlation_id
        assert result.content == ""
        assert result.error_message != ""
        assert result.model_used == "test-model"

    @pytest.mark.parametrize(
        "cli_url",
        ["cli://codex", "cli://claude", "CLI://codex", " cli://codex "],
    )
    def test_cli_scheme_fails_closed_no_subprocess(self, cli_url: str) -> None:
        """OMN-13215: the shelled-CLI inference path is removed.

        A ``cli://`` endpoint (config drift / a removed tier) must fail closed at
        the effect boundary with a typed error response — never spawn a subprocess.
        ``subprocess`` and ``shutil`` are no longer imported by the handler module,
        so any attempt to reach them would raise AttributeError; assert the handler
        returns a clean error response instead.
        """
        handler = HandlerInferenceIntent()
        intent = _make_intent(base_url=cli_url, model="codex-cli")

        # No httpx mock: a correctly-failing handler rejects the scheme BEFORE any
        # HTTP client is constructed.
        result = handler.handle(intent)

        assert isinstance(result, ModelInferenceResponseData)
        assert result.content == ""
        assert result.model_used == "codex-cli"
        assert "HTTP" in result.error_message
        assert "cli://" in result.error_message.lower() or cli_url.strip() in (
            result.error_message
        )

    def test_handler_module_has_no_subprocess_or_shutil(self) -> None:
        """OMN-13215: the codex-cli shell-execution code path is removed.

        Guard against reintroduction: the inference handler module must not import
        ``subprocess`` or ``shutil`` (the only consumers were the deleted CLI path).
        """
        import omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_inference_intent as mod

        assert not hasattr(mod, "subprocess")
        assert not hasattr(mod, "shutil")
        assert not hasattr(mod, "_call_cli_backend")
        assert not hasattr(mod, "_build_cli_command")
        assert not hasattr(mod, "_is_cli_backend")

    def test_ceiling_tier_http_endpoint_executes_like_lower_tiers(self) -> None:
        """OMN-13215/OMN-13351: the ceiling tier executes via the canonical HTTP path.

        A complete Gemini chat-completions URL (the default ceiling backend
        ``cloud-gemini-pro``, repointed off the dead Anthropic ``cloud-sonnet`` in
        OMN-13351) is posted verbatim with the resolved api_key, identical to every
        lower tier.
        """
        handler = HandlerInferenceIntent()
        ceiling_url = (
            "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
        )
        intent = _make_intent(
            base_url=ceiling_url,
            model="gemini-2.5-flash",
            api_key_ref="TEST_MODEL_API_KEY",
        )
        captured_urls: list[str] = []

        mock_response = MagicMock()
        mock_response.json.return_value = _SUCCESSFUL_HTTPX_RESPONSE
        mock_response.raise_for_status.return_value = None

        def _capture_post(url: str, **kwargs: object) -> MagicMock:
            captured_urls.append(url)
            return mock_response

        with (
            patch(
                "omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_inference_intent.resolve_api_key",
                return_value=None,
            ),
            patch("httpx.Client") as mock_client_cls,
        ):
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.side_effect = _capture_post
            mock_client_cls.return_value = mock_client

            result = handler.handle(intent)

        assert captured_urls == [ceiling_url]
        assert result.content == "def test_foo(): pass"
        assert result.model_used == "gemini-2.5-flash"
        assert result.error_message == ""

    def test_provider_http_status_error_includes_sanitized_body(self) -> None:
        handler = HandlerInferenceIntent()
        intent = _make_intent(
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
            model="gemini-2.5-flash",
            api_key_ref="TEST_MODEL_API_KEY",
        )
        request = httpx.Request(
            "POST",
            intent.base_url,
            headers={"Authorization": "Bearer sk-test-secret"},
        )
        response = httpx.Response(
            400,
            request=request,
            json={
                "error": {
                    "message": "unsupported request field: chat_template_kwargs",
                    "status": "INVALID_ARGUMENT",
                }
            },
        )

        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "bad request",
            request=request,
            response=response,
        )

        with (
            patch("httpx.Client") as mock_client_cls,
            patch(
                "omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_inference_intent.resolve_api_key"
            ) as mock_resolve_api_key,
        ):
            mock_resolve_api_key.return_value.get_secret_value.return_value = (
                "sk-test-secret"
            )
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = mock_response
            mock_client_cls.return_value = mock_client

            result = handler.handle(intent)

        assert result.content == ""
        assert result.model_used == "gemini-2.5-flash"
        assert "provider HTTP 400 Bad Request" in result.error_message
        assert "unsupported request field: chat_template_kwargs" in result.error_message
        assert "sk-test-secret" not in result.error_message
        assert "Authorization" not in result.error_message

    @pytest.mark.asyncio
    async def test_provider_timeout_publishes_inference_response_and_terminal_failure(
        self,
    ) -> None:
        workflow = HandlerDelegationWorkflow(workflows={})
        correlation_id = uuid4()
        workflow.handle_delegation_request(_make_delegation_request(correlation_id))
        decision = _make_terminal_routing_decision(correlation_id)
        inference_intents = workflow.handle_routing_decision(decision)
        assert len(inference_intents) == 1
        inference_intent = inference_intents[0]
        assert inference_intent.correlation_id == correlation_id
        assert inference_intent.base_url == decision.endpoint_url

        callback = _make_dispatch_callback(
            HandlerInferenceIntent(),  # type: ignore[arg-type]
            event_model=_inference_event_model_ref(),
        )
        envelope = ModelEventEnvelope[object](
            payload=inference_intent,
            correlation_id=correlation_id,
            envelope_timestamp=datetime.now(UTC),
            event_type="ModelInferenceIntent",
            payload_type="ModelInferenceIntent",
            source_tool="test-handler-inference-intent",
        )

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.side_effect = httpx.TimeoutException("provider timed out")
            mock_client_cls.return_value = mock_client

            dispatch_result = await callback(envelope)

        assert dispatch_result is not None
        assert dispatch_result.correlation_id == correlation_id
        assert len(dispatch_result.output_events) == 1
        response = dispatch_result.output_events[0]
        assert isinstance(response, ModelInferenceResponseData)
        assert response.correlation_id == correlation_id
        assert response.model_used == "Qwen3.6-27B"
        assert response.content == ""
        assert "provider timed out" in response.error_message

        bus = AsyncMock(spec=ProtocolEventBusLike)
        applier = DispatchResultApplier(
            event_bus=bus,
            output_topic="onex.evt.omnibase-infra.delegation-completed.v1",
            output_topic_map={"InferenceResponseData": TOPIC_INFERENCE_RESPONSE},
        )
        await applier.apply(dispatch_result, correlation_id=correlation_id)

        bus.publish_envelope.assert_awaited_once()
        publish_kwargs = bus.publish_envelope.call_args.kwargs
        assert publish_kwargs["topic"] == TOPIC_INFERENCE_RESPONSE
        published_response = publish_kwargs["envelope"].payload
        assert isinstance(published_response, ModelInferenceResponseData)
        assert published_response.correlation_id == correlation_id

        terminal_events = workflow.handle_inference_response(published_response)

        failure_event = next(
            event
            for event in terminal_events
            if isinstance(event, ModelDelegationEvent)
        )
        assert failure_event.topic == "onex.evt.omnibase-infra.delegation-failed.v1"
        assert workflow.workflows[correlation_id].state == EnumDelegationState.FAILED
        failure_payload = failure_event.payload
        assert isinstance(failure_payload, ModelDelegationResult)
        assert failure_payload.correlation_id == correlation_id
        assert failure_payload.quality_passed is False
        assert "provider timed out" in failure_payload.failure_reason

    @pytest.mark.parametrize("content", [None, "   "])
    def test_empty_message_content_returns_error_response(
        self, content: str | None
    ) -> None:
        handler = HandlerInferenceIntent()
        intent = _make_intent()

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "id": "chatcmpl-empty",
            "choices": [{"message": {"content": content}}],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 0,
                "total_tokens": 10,
            },
        }
        mock_response.raise_for_status.return_value = None

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = mock_response
            mock_client_cls.return_value = mock_client

            result = handler.handle(intent)

        assert result.correlation_id == intent.correlation_id
        assert result.content == ""
        assert result.model_used == "test-model"
        assert "empty message content" in result.error_message

    def test_length_finish_reason_returns_error_response(self) -> None:
        handler = HandlerInferenceIntent()
        intent = _make_intent()

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "id": "chatcmpl-truncated",
            "choices": [
                {
                    "finish_reason": "length",
                    "message": {"content": "def test_incomplete():\n    assert"},
                }
            ],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 20,
                "total_tokens": 30,
            },
        }
        mock_response.raise_for_status.return_value = None

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = mock_response
            mock_client_cls.return_value = mock_client

            result = handler.handle(intent)

        assert result.correlation_id == intent.correlation_id
        assert result.content == ""
        assert result.model_used == "test-model"
        assert "finish_reason=length" in result.error_message

    def test_returns_response_for_runtime_autopublish(self) -> None:
        # The runtime dispatch-result applier publishes the RETURNED model to the
        # contract's publish_topics; handle() must return ModelInferenceResponseData.
        handler = HandlerInferenceIntent()
        intent = _make_intent()

        mock_response = MagicMock()
        mock_response.json.return_value = _SUCCESSFUL_HTTPX_RESPONSE
        mock_response.raise_for_status.return_value = None

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = mock_response
            mock_client_cls.return_value = mock_client

            result = handler.handle(intent)

        assert isinstance(result, ModelInferenceResponseData)
        assert TOPIC_INFERENCE_RESPONSE.endswith("inference-response.v1")

    def test_system_prompt_included_in_messages(self) -> None:
        handler = HandlerInferenceIntent()
        intent = _make_intent(system_prompt="Be precise.")

        captured_payload: list[dict[str, object]] = []

        mock_response = MagicMock()
        mock_response.json.return_value = _SUCCESSFUL_HTTPX_RESPONSE
        mock_response.raise_for_status.return_value = None

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)

            def _capture_post(url: str, **kwargs: object) -> MagicMock:
                captured_payload.append(kwargs.get("json", {}))  # type: ignore[arg-type]
                return mock_response

            mock_client.post.side_effect = _capture_post
            mock_client_cls.return_value = mock_client

            handler.handle(intent)

        assert len(captured_payload) == 1
        messages = captured_payload[0].get("messages", [])
        assert messages[0]["role"] == "system"
        assert messages[0]["content"] == "Be precise."
        assert messages[1]["role"] == "user"

    def test_qwen_test_prompt_adds_reasoning_suppression_to_provider_payload(
        self,
    ) -> None:
        handler = HandlerInferenceIntent()
        original_prompt = "Write pytest unit tests for normalize_status."
        intent = _make_intent(
            model="Qwen3-Coder-30B",
            system_prompt="You are a test generation assistant.",
            prompt=original_prompt,
        )

        captured_payload: list[dict[str, object]] = []

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "id": "chatcmpl-qwen-no-think",
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "content": (
                            "import pytest\n\n"
                            "@pytest.mark.unit\n"
                            "def test_normalize_status():\n"
                            "    assert normalize_status('OK') == 'ok'\n"
                        )
                    },
                }
            ],
            "usage": {"prompt_tokens": 14, "completion_tokens": 31, "total_tokens": 45},
        }
        mock_response.raise_for_status.return_value = None

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)

            def _capture_post(url: str, **kwargs: object) -> MagicMock:
                captured_payload.append(kwargs.get("json", {}))  # type: ignore[arg-type]
                return mock_response

            mock_client.post.side_effect = _capture_post
            mock_client_cls.return_value = mock_client

            result = handler.handle(intent)

        assert "/no_think" not in intent.prompt
        assert result.error_message == ""
        assert "def test_normalize_status" in result.content
        assert result.prompt_tokens == 14
        messages = captured_payload[0]["messages"]
        assert messages[1]["content"] == f"/no_think\n{original_prompt}"

    def test_provider_request_options_are_merged_into_provider_payload(self) -> None:
        handler = HandlerInferenceIntent()
        intent_kwargs: dict[str, object] = {
            "model": "Qwen3.6-35B-A3B",
            "system_prompt": "You are a production-quality code generation assistant.",
        }
        if "provider_request_options" in getattr(
            ModelInferenceIntent, "model_fields", {}
        ):
            intent_kwargs["provider_request_options"] = {
                "chat_template_kwargs": {"enable_thinking": False}
            }
        intent = _make_intent(**intent_kwargs)

        captured_payload: list[dict[str, object]] = []

        mock_response = MagicMock()
        mock_response.json.return_value = _SUCCESSFUL_HTTPX_RESPONSE
        mock_response.raise_for_status.return_value = None

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)

            def _capture_post(url: str, **kwargs: object) -> MagicMock:
                captured_payload.append(kwargs.get("json", {}))  # type: ignore[arg-type]
                return mock_response

            mock_client.post.side_effect = _capture_post
            mock_client_cls.return_value = mock_client

            result = handler.handle(intent)

        assert result.error_message == ""
        assert captured_payload[0]["chat_template_kwargs"] == {"enable_thinking": False}

    def test_api_key_ref_resolved_at_effect_boundary(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TEST_MODEL_API_KEY", "sk-test-key")
        handler = HandlerInferenceIntent()
        intent = _make_intent(api_key_ref="TEST_MODEL_API_KEY")

        captured_headers: list[dict[str, str]] = []

        mock_response = MagicMock()
        mock_response.json.return_value = _SUCCESSFUL_HTTPX_RESPONSE
        mock_response.raise_for_status.return_value = None

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)

            def _capture_post(url: str, **kwargs: object) -> MagicMock:
                captured_headers.append(kwargs.get("headers") or {})  # type: ignore[arg-type]
                return mock_response

            mock_client.post.side_effect = _capture_post
            mock_client_cls.return_value = mock_client

            handler.handle(intent)

        assert len(captured_headers) == 1
        assert captured_headers[0].get("Authorization") == "Bearer sk-test-key"

    @pytest.mark.asyncio
    async def test_api_key_ref_resolves_through_runtime_async_dispatch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TEST_MODEL_API_KEY", "sk-test-key")
        correlation_id = uuid4()
        intent = _make_intent(
            correlation_id=correlation_id,
            api_key_ref="TEST_MODEL_API_KEY",
        )
        callback = _make_dispatch_callback(
            HandlerInferenceIntent(),  # type: ignore[arg-type]
            event_model=_inference_event_model_ref(),
        )
        envelope = ModelEventEnvelope[object](
            payload=intent,
            correlation_id=correlation_id,
            envelope_timestamp=datetime.now(UTC),
            event_type="ModelInferenceIntent",
            payload_type="ModelInferenceIntent",
            source_tool="test-handler-inference-intent",
        )
        captured_headers: list[dict[str, str]] = []

        mock_response = MagicMock()
        mock_response.json.return_value = _SUCCESSFUL_HTTPX_RESPONSE
        mock_response.raise_for_status.return_value = None

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)

            def _capture_post(url: str, **kwargs: object) -> MagicMock:
                captured_headers.append(kwargs.get("headers") or {})  # type: ignore[arg-type]
                return mock_response

            mock_client.post.side_effect = _capture_post
            mock_client_cls.return_value = mock_client

            dispatch_result = await callback(envelope)

        assert dispatch_result is not None
        assert len(dispatch_result.output_events) == 1
        response = dispatch_result.output_events[0]
        assert isinstance(response, ModelInferenceResponseData)
        assert response.error_message == ""
        assert response.content == "def test_foo(): pass"
        assert captured_headers == [{"Authorization": "Bearer sk-test-key"}]

    def test_missing_api_key_ref_returns_error_response(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("TEST_MODEL_API_KEY", raising=False)
        handler = HandlerInferenceIntent()
        intent = _make_intent(api_key_ref="TEST_MODEL_API_KEY")

        result = handler.handle(intent)

        assert result.correlation_id == intent.correlation_id
        assert result.content == ""
        assert "TEST_MODEL_API_KEY" in result.error_message

    def test_contract_declares_inference_response_publish_topic(self) -> None:
        from pathlib import Path

        from omnimarket.nodes.contract_topics import contract_publish_topics

        contract_path = (
            Path(__file__).parent.parent.parent.parent
            / "src/omnimarket/nodes/node_llm_delegation_call_effect/contract.yaml"
        )
        topics = contract_publish_topics(contract_path)
        assert TOPIC_INFERENCE_RESPONSE in topics
