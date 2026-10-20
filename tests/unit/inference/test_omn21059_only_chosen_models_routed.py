# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21059 AC2: a key's route runs the models its owner chose, in their order, and no other.

A customer chose two models for one key; the key's own model list also names a
cheaper model the catalogue prefers, which they did not choose. The route runs
the first chosen model, a failure moves to the second on the same key, and the
walk ends there. Before this, a Gemini, OpenAI, Anthropic or z.ai key's call
that was throttled or not found re-aimed itself at the cheapest preferred model
on the list, a model the customer never chose.

Fakes only: the provider's model list and the chat call are stubs; the key and
its ordered list are stored the way ``onex models add`` stores them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from omnimarket.enums.enum_secret_source import EnumSecretSource
from omnimarket.events.llm_delegation_call import ModelLlmDelegationCallRequest
from omnimarket.inference.local_byok_credential_adapter import (
    resolve_local_byok_credential_model,
    resolve_local_byok_credential_models,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers import (
    handler_llm_delegation_call as effect,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers import transport
from omnimarket.nodes.node_local_secret_store_effect.handlers.handler_local_secret_store import (
    HandlerLocalSecretStore,
)
from omnimarket.nodes.node_local_secret_store_effect.models.model_local_secret_request import (
    ModelLocalSecretRequest,
)
from omnimarket.routing import byok_model_discovery
from omnimarket.routing.byok_provider_backends import (
    catalogue_prefers_model,
    resolve_byok_backend_by_id,
)
from omnimarket.routing.delegation_backend_resolution import (
    ModelResolvedDelegationBackend,
)
from omnimarket.routing.local_byok_route import next_chosen_model_byok_route
from tests.chains.local.harness import store_model_credential, use_local_store

pytestmark = pytest.mark.unit

#: backend id -> (the cheaper model the catalogue prefers, the customer's
#: first choice, their second). The cheaper model is on the key's list and is
#: never chosen.
_CASES: dict[str, tuple[str, str, str]] = {
    "byok-gemini": ("gemini-3.5-flash-lite", "gemini-3.8-flash", "gemini-3.5-pro"),
    "byok-openai": ("gpt-4.1-nano", "gpt-4.1-mini", "gpt-5.5"),
    "byok-anthropic": ("claude-haiku-4-5", "claude-sonnet-4-5", "claude-opus-4-1"),
    "byok-glm-general": ("glm-5.3-flash", "glm-5.3", "glm-5.3-air"),
    "byok-openrouter": (
        "google/gemma-4-31b-it:free",
        "qwen/qwen3.8-27b",
        "openai/gpt-5-nano",
    ),
}


def _row(backend_id: str) -> Any:
    row = resolve_byok_backend_by_id(backend_id)
    assert row is not None
    return row


def test_each_case_names_a_cheaper_model_the_catalogue_prefers() -> None:
    for backend_id, (cheaper, _first, _second) in _CASES.items():
        assert catalogue_prefers_model(_row(backend_id), cheaper), backend_id


@pytest.mark.parametrize("backend_id", sorted(_CASES))
def test_the_route_walks_exactly_the_chosen_models_in_order(
    backend_id: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path = use_local_store(monkeypatch, tmp_path)
    row = _row(backend_id)
    cheaper, first, second = _CASES[backend_id]
    HandlerLocalSecretStore().handle(
        ModelLocalSecretRequest(
            operation="set",
            secret_ref=f"llm.{row.provider}.api_key",
            value=SecretStr(f"synthetic-omn21059-{row.provider}-key"),
            plan=row.plan,
            models=(first, second),
            force=True,
        )
    )

    assert resolve_local_byok_credential_models(row.provider, db_path=db_path) == (
        first,
        second,
    )
    route = ModelResolvedDelegationBackend(
        backend_id=row.backend_id,
        model_id=resolve_local_byok_credential_model(row.provider, db_path=db_path)
        or "",
        endpoint_ref=row.endpoint_url,
        tier="cheap_cloud",
        max_tokens=256,
        timeout_ms=60000,
    )
    plan = [route.model_id]
    tried: frozenset[tuple[str, str]] = frozenset()
    while (
        nxt := next_chosen_model_byok_route(route, tried_models=tried, db_path=db_path)
    ) is not None:
        tried |= {(route.backend_id, route.model_id)}
        assert nxt.backend_id == row.backend_id
        plan.append(nxt.model_id)
        route = nxt

    assert plan == [first, second]
    assert cheaper not in plan


class _Chat:
    """The provider's chat endpoint: every POST is throttled (429)."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def post(
        self, *, endpoint_url: str, payload: dict[str, Any], **_: Any
    ) -> transport.ModelTransportResponse:
        self.calls.append(payload["model"])
        request = httpx.Request("POST", endpoint_url)
        response = httpx.Response(
            429,
            request=request,
            json={"error": {"message": f"{payload['model']} is rate-limited"}},
        )
        response.raise_for_status()
        raise AssertionError("unreachable: a 429 raises")


class _Models:
    def __init__(self, listed: tuple[str, ...]) -> None:
        self.listed = listed
        self.calls: list[str] = []

    def __call__(self, *, url: str, **_: Any) -> Any:
        self.calls.append(url)
        return {"data": [{"id": model} for model in self.listed]}


@pytest.mark.parametrize("backend_id", sorted(_CASES))
def test_a_throttled_chosen_model_is_never_re_aimed_at_an_unchosen_one(
    backend_id: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path = use_local_store(monkeypatch, tmp_path)
    row = _row(backend_id)
    cheaper, first, second = _CASES[backend_id]
    ref = f"cred_acme_{row.provider}_{'c' * 32}"
    store_model_credential(
        db_path, ref, provider=row.provider, model=first, model_chosen_by="customer"
    )
    chat = _Chat()
    models = _Models((cheaper, first, second))
    monkeypatch.setattr(transport, "post_chat_completion", chat.post)
    monkeypatch.setattr(byok_model_discovery, "get_models_json", models)
    monkeypatch.setattr(effect, "_is_endpoint_healthy", lambda _: True)
    monkeypatch.setattr(effect, "_get_served_model_ids", lambda _: None)
    monkeypatch.setattr(
        effect,
        "resolve_api_key_with_source_loop_safe",
        lambda *_args, **_kwargs: (
            SecretStr("synthetic-omn21059-call-key"),
            EnumSecretSource.LOCAL_STORE,
        ),
    )

    result = effect.HandlerLlmDelegationCall().__call__(
        ModelLlmDelegationCallRequest(
            request_id=str(uuid4()),
            correlation_id=str(uuid4()),
            causation_id=str(uuid4()),
            model_id=first,
            endpoint_ref=row.endpoint_url,
            prompt="say hi",
            prompt_hash="0" * 64,
            timeout_seconds=5,
            secret_ref=ref,
            api_key_env=None,
        )
    )

    assert not result.success
    assert chat.calls == [first]
    assert cheaper not in chat.calls
    assert models.calls == []
    assert result.earlier_model_attempts == ()
