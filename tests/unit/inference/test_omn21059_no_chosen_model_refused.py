# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21059 AC3: a key with no model its owner chose is refused, on every provider.

Before this, only the OpenRouter row had the customer choose. A Gemini, OpenAI,
Anthropic or z.ai key registered with no ``--model`` got the cheapest model the
catalogue preferred among the key's list, a key whose list could not be read
was given one at its first delegation, and a call on the catalogue's pick moved
to another preferred model on a throttle. Now every catalogue row has the
customer choose:

* ``onex models add <provider>`` from a script with no ``--model`` exits
  non-zero with ``BYOK_MODEL_NOT_CHOSEN`` and the command that chooses one,
  stores nothing, and reads no model list;
* a delegation on a key holding no model, or holding a model the catalogue
  picked, is refused with the same code before any provider call, and no model
  is looked up for it;
* the hosted intake refuses a key with no model the same way.

Fakes only: the provider's model list and the chat call are stubs.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from click.testing import CliRunner
from pydantic import SecretStr

from omnimarket.cli.cli_secret import models_group
from omnimarket.enums.enum_delegation_failure_class import (
    EnumDelegationFailureClass,
)
from omnimarket.enums.enum_secret_source import EnumSecretSource
from omnimarket.events.llm_delegation_call import ModelLlmDelegationCallRequest
from omnimarket.inference.local_byok_credential_adapter import (
    LocalByokCredentialStore,
    resolve_local_byok_credential_model,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers import (
    handler_llm_delegation_call as effect,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers import transport
from omnimarket.nodes.node_model_setup_effect.handlers import handler_model_setup
from omnimarket.projection.credential_publisher import (
    CredentialKeyRefusedError,
    ModelInferenceCredentialCreateRequest,
    register_inference_credential,
)
from omnimarket.routing import byok_model_discovery
from omnimarket.routing.byok_model_discovery import (
    BYOK_MODEL_NOT_CHOSEN,
    discover_byok_model_sync,
)
from omnimarket.routing.byok_provider_backends import (
    BYOK_MODEL_UNRESOLVED,
    load_byok_plan_catalog,
    resolve_byok_backend_by_id,
)
from tests.chains.local.harness import store_model_credential, use_local_store

pytestmark = pytest.mark.unit

#: provider -> (a key of that provider's shape, the models its list names, the
#: cheapest of them the catalogue prefers). Synthetic; never printed.
_KEYS: dict[str, tuple[str, tuple[str, ...]]] = {
    "gemini": (
        "AIza" + "syntheticOMN21059" * 2,
        ("gemini-3.5-flash-lite", "gemini-3.8-flash"),
    ),
    "openai": ("sk-proj-synthetic-omn21059", ("gpt-4.1-nano", "gpt-4.1-mini")),
    "anthropic": (
        "sk-ant-synthetic-omn21059",
        ("claude-haiku-4-5", "claude-sonnet-4-5"),
    ),
    "openrouter": (
        "sk-or-v1-synthetic-omn21059",
        ("google/gemma-4-31b-it:free", "qwen/qwen3.8-27b"),
    ),
}
#: Every routable catalogue row, by backend id: the call-time refusal holds on
#: each, not only on the providers ``onex models add`` offers.
_ROUTES = (
    "byok-openrouter",
    "byok-gemini",
    "byok-openai",
    "byok-anthropic",
    "byok-glm-general",
)


class _Models:
    """The provider's list-models endpoint: records each read."""

    def __init__(self, listed: tuple[str, ...]) -> None:
        self.listed = listed
        self.calls: list[str] = []

    def __call__(self, *, url: str, **_: Any) -> Any:
        self.calls.append(url)
        return {"data": [{"id": model} for model in self.listed]}


class _Chat:
    """The provider's chat endpoint: records each POST and answers it."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def post(
        self, *, endpoint_url: str, payload: dict[str, Any], **_: Any
    ) -> transport.ModelTransportResponse:
        self.calls.append((endpoint_url, payload["model"]))
        return transport.ModelTransportResponse(
            status_code=200,
            json_body={
                "choices": [
                    {
                        "message": {"content": "### ANSWER\nok\n"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
            latency_ms=1,
        )


@pytest.fixture
def db_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    path = use_local_store(monkeypatch, tmp_path)
    monkeypatch.setattr(
        handler_model_setup, "default_evidence_db_path", lambda: tmp_path / "evidence"
    )
    return path


@pytest.fixture
def pinned_tests(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The pinned test delegation ``onex models add`` runs; never a real one."""
    pins: list[str] = []

    def _delegate(backend_id: str) -> tuple[int, str, str]:
        pins.append(backend_id)
        return 1, "", "no test delegation in this test"

    monkeypatch.setattr(handler_model_setup, "run_pinned_delegation", _delegate)
    return pins


@pytest.mark.parametrize("provider", sorted(_KEYS))
def test_models_add_from_a_script_with_no_model_is_refused_and_stores_nothing(
    provider: str,
    db_path: Path,
    pinned_tests: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key, listed = _KEYS[provider]
    models = _Models(listed)
    monkeypatch.setattr(byok_model_discovery, "get_models_json", models)

    result = CliRunner().invoke(models_group, ["add", provider, "--json"], input=key)

    assert result.exit_code != 0, result.output
    assert BYOK_MODEL_NOT_CHOSEN in result.output
    assert f"onex models add {provider} --model" in result.output
    assert models.calls == []
    assert pinned_tests == []
    assert resolve_local_byok_credential_model(provider, db_path=db_path) is None
    stored = asyncio.run(
        LocalByokCredentialStore().get_secret(f"llm.{provider}.api_key")
    )
    assert stored is None
    assert key not in result.output


def _route_request(
    backend_id: str, model: str, ref: str
) -> ModelLlmDelegationCallRequest:
    row = resolve_byok_backend_by_id(backend_id)
    assert row is not None
    return ModelLlmDelegationCallRequest(
        request_id=str(uuid4()),
        correlation_id=str(uuid4()),
        causation_id=str(uuid4()),
        model_id=model,
        endpoint_ref=row.endpoint_url,
        prompt="say hi",
        prompt_hash="0" * 64,
        timeout_seconds=5,
        secret_ref=ref,
        api_key_env=None,
    )


def _bind_call(monkeypatch: pytest.MonkeyPatch) -> tuple[_Chat, _Models]:
    chat = _Chat()
    models = _Models(("cheapest-preferred-model", "another-model"))
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
    return chat, models


def _ref(backend_id: str) -> str:
    return f"cred_acme_{backend_id.removeprefix('byok-')}_{'b' * 32}"


@pytest.mark.parametrize("backend_id", _ROUTES)
def test_a_key_holding_no_model_is_refused_before_any_call(
    backend_id: str, db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chat, models = _bind_call(monkeypatch)

    result = effect.HandlerLlmDelegationCall().__call__(
        _route_request(backend_id, BYOK_MODEL_UNRESOLVED, _ref(backend_id))
    )

    assert not result.success
    assert result.failure_class is EnumDelegationFailureClass.PROVIDER_MODEL_NOT_FOUND
    assert BYOK_MODEL_NOT_CHOSEN in (result.error_message or "")
    assert "--model" in (result.error_message or "")
    assert chat.calls == []
    assert models.calls == []


@pytest.mark.parametrize("backend_id", _ROUTES)
def test_a_key_holding_the_catalogues_pick_is_refused_before_any_call(
    backend_id: str, db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chat, models = _bind_call(monkeypatch)
    ref = _ref(backend_id)
    row = resolve_byok_backend_by_id(backend_id)
    assert row is not None
    store_model_credential(
        db_path,
        ref,
        provider=row.provider,
        model="cheapest-preferred-model",
        model_chosen_by="catalogue",
    )

    result = effect.HandlerLlmDelegationCall().__call__(
        _route_request(backend_id, "cheapest-preferred-model", ref)
    )

    assert not result.success
    assert BYOK_MODEL_NOT_CHOSEN in (result.error_message or "")
    assert chat.calls == []
    assert models.calls == []


def test_every_routable_catalogue_row_has_the_customer_choose() -> None:
    rows = [row for row in load_byok_plan_catalog().values() if row.customer_routable]

    assert rows
    assert [row.backend_id for row in rows if not row.customer_chooses_model] == []


@pytest.mark.parametrize(
    ("provider", "plan"),
    [("gemini", None), ("openai", None), ("anthropic", None), ("glm", "general_api")],
)
def test_the_hosted_intake_refuses_a_key_with_no_model(
    provider: str, plan: str | None
) -> None:
    class _Store:
        async def set_secret(self, ref: str, value: str) -> bool:
            raise AssertionError("a refused key is never stored")

        async def close(self) -> None:
            return None

    class _Bus:
        async def start(self) -> None:
            return None

        async def publish_envelope(self, *args: Any, **kwargs: Any) -> None:
            raise AssertionError("a refused key publishes nothing")

        async def close(self) -> None:
            return None

    looked_up: list[str] = []

    async def _discover(backend: Any, key: Any, **kwargs: Any) -> Any:
        looked_up.append(backend.backend_id)
        return discover_byok_model_sync(
            backend, key, get=_Models(("cheapest-preferred-model",)), **kwargs
        )

    with pytest.raises(CredentialKeyRefusedError, match=BYOK_MODEL_NOT_CHOSEN):
        asyncio.run(
            register_inference_credential(
                ModelInferenceCredentialCreateRequest(
                    name=provider,
                    provider=provider,
                    key_value=SecretStr("synthetic-omn21059-hosted-key"),
                    plan=plan,
                ),
                tenant_id="acme",
                secret_store=cast(Any, _Store()),
                event_bus=cast(Any, _Bus()),
                model_discoverer=_discover,
            )
        )
    assert looked_up == []
