# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Test OpenAI max_completion_tokens vs max_tokens in chat-completions payloads."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import httpx
import pytest
from omnibase_core.models.delegation.wire import (
    EnumCredentialSource,
    ModelInferenceIntent,
)

from omnimarket.events.llm_delegation_call import ModelLlmDelegationCallRequest
from omnimarket.nodes.node_llm_delegation_call_effect.handlers import (
    handler_inference_intent as bus_effect,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers import (
    handler_llm_delegation_call as effect,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers import transport
from omnimarket.routing.byok_provider_backends import (
    CATALOG_PATH,
    resolve_byok_provider_backend,
)

pytestmark = [pytest.mark.unit]
KEY = "test-key-omn21257"
LOCAL_URL = "http://127.0.0.1:11434/v1/chat/completions"
OK_BODY = {
    "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
}


@pytest.fixture(autouse=True)
def _no_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(effect, "_is_endpoint_healthy", lambda _: True)
    monkeypatch.setattr(effect, "_get_served_model_ids", lambda _: None)
    monkeypatch.setattr(transport, "get_served_model_ids", lambda *_a, **_k: None)


class FakeClient:
    """Stands in for ``httpx.Client`` in the bus handler, recording each body."""

    def __init__(self, seen: list[dict[str, Any]]) -> None:
        self.seen = seen

    def __call__(self, **_: Any) -> FakeClient:
        return self

    def __enter__(self) -> FakeClient:
        return self

    def __exit__(self, *_: Any) -> None:
        return None

    def post(
        self, url: str, *, json: dict[str, Any], headers: dict[str, str], **_: Any
    ) -> httpx.Response:
        self.seen.append(json)
        return httpx.Response(200, request=httpx.Request("POST", url), json=OK_BODY)


def _url_for_case(case: str) -> str:
    if case == "local":
        return LOCAL_URL
    row = resolve_byok_provider_backend(case)
    assert row is not None
    return row.endpoint_url


def _expected_key_for_case(case: str) -> str:
    return "max_completion_tokens" if case == "openai" else "max_tokens"


def _other_key(expected_key: str) -> str:
    return (
        "max_tokens"
        if expected_key == "max_completion_tokens"
        else "max_completion_tokens"
    )


@pytest.mark.parametrize("case", ["openai", "openrouter", "gemini", "local"])
def test_effect_handler_uses_expected_token_limit_key(
    case: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = _url_for_case(case)
    expected_key = _expected_key_for_case(case)
    other_key = _other_key(expected_key)
    seen: list[dict[str, Any]] = []

    def fake_post_chat_completion(
        *, endpoint_url: str, payload: dict[str, Any], **_kwargs: Any
    ) -> transport.ModelTransportResponse:
        seen.append(payload)
        return transport.ModelTransportResponse(
            status_code=200, json_body=OK_BODY, latency_ms=1
        )

    monkeypatch.setattr(transport, "post_chat_completion", fake_post_chat_completion)
    request = ModelLlmDelegationCallRequest(
        request_id=str(uuid4()),
        correlation_id=str(uuid4()),
        causation_id=str(uuid4()),
        model_id="m",
        endpoint_ref=url,
        prompt="say hi",
        prompt_hash="0" * 64,
        timeout_seconds=5,
        max_tokens=64,
        secret_ref=None,
        api_key_env=None,
    )

    effect.HandlerLlmDelegationCall()._post_one_call(request, url, None)

    assert len(seen) == 1
    payload = seen[0]
    assert payload[expected_key] == 64
    assert other_key not in payload


@pytest.mark.parametrize("case", ["openai", "openrouter", "gemini", "local"])
def test_bus_handler_uses_expected_token_limit_key(
    case: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = _url_for_case(case)
    expected_key = _expected_key_for_case(case)
    other_key = _other_key(expected_key)
    seen: list[dict[str, Any]] = []

    monkeypatch.setattr(httpx, "Client", FakeClient(seen))
    intent = ModelInferenceIntent(
        base_url=url,
        model="m",
        system_prompt="s",
        prompt="p",
        max_tokens=64,
        correlation_id=uuid4(),
        tenant_id="acme",
        api_key_ref=None,
    )

    bus_effect.HandlerInferenceIntent()._call_llm_on_resolved_model(
        intent,
        "call-omn21257",
        api_key=KEY,
        credential_source=EnumCredentialSource.CUSTOMER_KEY,
    )

    assert len(seen) == 1
    payload = seen[0]
    assert payload[expected_key] == 64
    assert other_key not in payload


def test_openai_row_declares_max_completion_tokens_and_drops_the_exclusion_note() -> (
    None
):
    row = resolve_byok_provider_backend("openai")
    assert row is not None
    assert row.token_limit_field == "max_completion_tokens"
    for provider in ("openrouter", "gemini"):
        other = resolve_byok_provider_backend(provider)
        assert other is not None
        assert other.token_limit_field == "max_tokens"
    catalogue = CATALOG_PATH.read_text(encoding="utf-8")
    assert "NOT preferred on purpose" not in catalogue
