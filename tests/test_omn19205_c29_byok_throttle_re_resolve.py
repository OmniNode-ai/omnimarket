# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19205: a customer-key delegation survives one throttled or down free slug.

C29 graded red when the one free OpenRouter slug its key resolved answered 429
(or an in-body "Upstream error from ..."). The customer call now re-resolves
ONCE from the key's own model list, excluding the failed model, on the same
backend with the same key. Fakes only, no network.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from omnimarket.enums.enum_delegation_failure_class import (
    EnumDelegationFailureClass as Failure,
)
from omnimarket.enums.enum_secret_source import EnumSecretSource
from omnimarket.events.llm_delegation_call import ModelLlmDelegationCallRequest
from omnimarket.inference import local_byok_credential_adapter as credentials
from omnimarket.nodes.node_llm_delegation_call_effect.handlers import (
    handler_llm_delegation_call as effect,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers import transport
from omnimarket.routing import byok_model_discovery as discovery
from tests.chains.local.harness import (
    house_openrouter_rung,
    run_local_delegation,
    use_local_store,
)
from tests.chains.local.test_chain_l6_typed_credential_refusals_omn18698 import (
    _ladder_with_every_successor,
)
from tests.test_omn20157_byok_model_discovery_and_typed_refusals import (
    SUCCESSOR_URL,
    FakeModels,
    _isolated_effects,
    _row,
)

pytestmark = pytest.mark.unit

# The OMN-20157 autouse fixture (local store, healthy endpoint) applies here too.
_SHARED_AUTOUSE_FIXTURE = _isolated_effects

KEY = "synthetic-omn19205-key-asserted-absent-from-outputs"
REF = f"cred_acme_openrouter_{'a' * 32}"
GEMMA = "google/gemma-4-31b-it:free"
NEMOTRON = "nvidia/nemotron-3-ultra-550b-a55b:free"
THROTTLE = (
    "Client error '429 Too Many Requests' for url "
    "'https://openrouter.ai/api/v1/chat/completions' "
    "google/gemma-4-31b-it:free is temporarily rate-limited upstream"
)
UPSTREAM_DOWN = {
    "error": {
        "code": 503,
        "message": "Upstream error from Nvidia: Service temporarily overloaded",
        "metadata": {"error_type": "provider_unavailable"},
    }
}
OK_BODY = {
    "choices": [
        {"message": {"content": "### ANSWER\nprint('ok')\n"}, "finish_reason": "stop"}
    ],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
}


def _openrouter() -> Any:
    return _row("openrouter")


class FakeOpenRouter:
    """One scripted answer per POST: an HTTP status, a 200 body, or success."""

    def __init__(self, script: list[int | dict[str, Any]]) -> None:
        self.script = iter(script)
        self.calls: list[tuple[str, str]] = []

    def post(
        self,
        *,
        endpoint_url: str,
        payload: dict[str, Any],
        extra_headers: dict[str, str],
        **_: Any,
    ) -> transport.ModelTransportResponse:
        self.calls.append((endpoint_url, payload["model"]))
        if endpoint_url == SUCCESSOR_URL:
            response = httpx.Response(
                503, request=httpx.Request("POST", endpoint_url), json={}
            )
            response.raise_for_status()
        assert extra_headers.get("Authorization") == f"Bearer {KEY}"
        step = next(self.script)  # An unexpected extra POST fails the test.
        if isinstance(step, dict):
            return transport.ModelTransportResponse(
                status_code=200, json_body=step, latency_ms=1
            )
        request = httpx.Request("POST", endpoint_url)
        response = httpx.Response(
            step,
            request=request,
            json=OK_BODY if step == 200 else {"error": {"message": THROTTLE}},
        )
        response.raise_for_status()
        return transport.ModelTransportResponse(
            status_code=200, json_body=response.json(), latency_ms=1
        )


def _bind(
    monkeypatch: pytest.MonkeyPatch,
    script: list[int | dict[str, Any]],
    listed: list[str],
) -> tuple[FakeOpenRouter, FakeModels]:
    provider = FakeOpenRouter(script)
    models = FakeModels({"data": [{"id": model} for model in listed]})
    monkeypatch.setattr(transport, "post_chat_completion", provider.post)
    monkeypatch.setattr(discovery, "get_models_json", models)
    monkeypatch.setattr(
        effect,
        "resolve_api_key_with_source_loop_safe",
        lambda *_args, **_kwargs: (SecretStr(KEY), EnumSecretSource.LOCAL_STORE),
    )
    return provider, models


def _request(*, ref: str = REF) -> ModelLlmDelegationCallRequest:
    return ModelLlmDelegationCallRequest(
        request_id=str(uuid4()),
        correlation_id=str(uuid4()),
        causation_id=str(uuid4()),
        model_id=GEMMA,
        endpoint_ref=_openrouter().endpoint_url,
        prompt="say hi",
        prompt_hash="0" * 64,
        timeout_seconds=5,
        secret_ref=ref,
        api_key_env=None,
    )


@pytest.mark.parametrize("first", [429, UPSTREAM_DOWN], ids=["429", "in-body-503"])
def test_a_throttled_or_down_slug_re_resolves_once_and_the_second_model_answers(
    monkeypatch: pytest.MonkeyPatch, first: int | dict[str, Any]
) -> None:
    provider, models = _bind(monkeypatch, [first, 200], [GEMMA, NEMOTRON])
    result = effect.HandlerLlmDelegationCall().__call__(_request())
    assert result.success, result.error_message
    url = _openrouter().endpoint_url
    assert provider.calls == [(url, GEMMA), (url, NEMOTRON)]
    assert [call["url"] for call in models.calls] == [_openrouter().models_url]
    assert result.served_model_id == NEMOTRON
    assert [a.model_id for a in result.earlier_model_attempts] == [GEMMA]
    assert KEY not in str(result.model_dump())


def test_both_models_throttled_is_a_typed_refusal_naming_both(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, _ = _bind(monkeypatch, [429, 429], [GEMMA, NEMOTRON])
    result = effect.HandlerLlmDelegationCall().__call__(_request())
    assert not result.success
    assert result.failure_class is Failure.RATE_LIMITED
    assert GEMMA in result.error_message
    assert NEMOTRON in result.error_message
    assert len(provider.calls) == 2
    assert KEY not in str(result.model_dump())


def test_a_throttle_with_no_other_listed_model_is_the_typed_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, models = _bind(monkeypatch, [429], [GEMMA])
    result = effect.HandlerLlmDelegationCall().__call__(_request())
    assert not result.success
    assert result.failure_class is Failure.RATE_LIMITED
    assert GEMMA in result.error_message
    assert len(provider.calls) == 1
    assert len(models.calls) == 1


def test_a_house_route_is_never_re_resolved_on_a_throttle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, models = _bind(monkeypatch, [429], [GEMMA, NEMOTRON])
    result = effect.HandlerLlmDelegationCall().__call__(
        _request(ref="llm.openrouter.api_key")
    )
    assert result.failure_class is Failure.RATE_LIMITED
    assert [call[1] for call in provider.calls] == [GEMMA]
    assert models.calls == []


def test_a_404_still_re_resolves_exactly_as_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, _ = _bind(monkeypatch, [404, 200], [GEMMA, NEMOTRON])
    result = effect.HandlerLlmDelegationCall().__call__(_request())
    assert result.success, result.error_message
    assert [call[1] for call in provider.calls] == [GEMMA, NEMOTRON]


async def test_the_receipt_carries_one_rejected_and_one_accepted_attempt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The C29 ``names_provider`` shape: one accept, whose model is the receipt's."""
    provider, _ = _bind(monkeypatch, [429, 200], [GEMMA, NEMOTRON])
    db_path = use_local_store(monkeypatch, tmp_path)
    from omnimarket.inference.secret_store_resolver import (
        resolve_api_key_with_source_loop_safe,
    )

    monkeypatch.setattr(
        effect,
        "resolve_api_key_with_source_loop_safe",
        resolve_api_key_with_source_loop_safe,
    )
    credentials.register_local_byok_credential(
        "openrouter", KEY, model=GEMMA, db_path=db_path
    )
    _ladder_with_every_successor(
        monkeypatch,
        first_rung=house_openrouter_rung(monkeypatch),
        next_rung_url=SUCCESSOR_URL,
    )
    response = await run_local_delegation(
        prompt="write a function that parses a semver string",
        db_path=db_path,
        correlation_id=uuid4(),
    )
    assert response.status == "completed", response.model_dump()
    row = _openrouter()
    assert [call[1] for call in provider.calls if call[0] == row.endpoint_url] == [
        GEMMA,
        NEMOTRON,
    ]
    mine = [a for a in response.attempts if a.backend_id == row.backend_id]
    accepted = [a for a in mine if a.acceptance_decision == "accept"]
    rejected = [a for a in mine if a.acceptance_decision != "accept"]
    assert [a.model_id for a in accepted] == [NEMOTRON]
    assert [a.model_id for a in rejected] == [GEMMA]
    assert rejected[0].failure_class == Failure.RATE_LIMITED.value
    assert KEY not in str(response.model_dump())
