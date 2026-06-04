# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for HandlerInferenceIntent (OMN-12294).

Verifies the contract-native inference handler that replaces the in-process
DelegationIntentBridge.handle_inference_intent() path.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from omnibase_core.models.delegation.wire import (
    ModelInferenceIntent,
    ModelInferenceResponseData,
)

from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_inference_intent import (
    TOPIC_INFERENCE_RESPONSE,
    HandlerInferenceIntent,
)


def _make_intent(**kwargs: object) -> ModelInferenceIntent:
    defaults: dict[str, object] = {
        "base_url": "http://localhost:8000",
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

    def test_api_key_added_to_headers(self) -> None:
        handler = HandlerInferenceIntent()
        intent = _make_intent(api_key="sk-test-key")

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

    def test_contract_declares_inference_response_publish_topic(self) -> None:
        from pathlib import Path

        from omnimarket.nodes.contract_topics import contract_publish_topics

        contract_path = (
            Path(__file__).parent.parent.parent.parent
            / "src/omnimarket/nodes/node_llm_delegation_call_effect/contract.yaml"
        )
        topics = contract_publish_topics(contract_path)
        assert TOPIC_INFERENCE_RESPONSE in topics
