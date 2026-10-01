# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20157: per-key discovery and terminal provider refusals, with fakes only."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from click.testing import CliRunner
from omnibase_core.models.delegation.wire import ModelInferenceIntent
from omnibase_infra.event_bus.event_bus_kafka import EventBusKafka
from pydantic import SecretStr

from omnimarket.cli import cli_secret
from omnimarket.enums.enum_delegation_failure_class import (
    EnumDelegationFailureClass as Failure,
)
from omnimarket.enums.enum_secret_source import EnumSecretSource
from omnimarket.events.llm_delegation_call import ModelLlmDelegationCallRequest
from omnimarket.inference import local_byok_credential_adapter as credentials
from omnimarket.inference.provider_response_error import (
    describe_provider_refusal,
    failure_class_for_status,
)
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    _inference_error_failure_class,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers import (
    handler_inference_intent as bus_effect,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers import (
    handler_llm_delegation_call as effect,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers import transport
from omnimarket.projection.credential_publisher import (
    CredentialKeyRefusedError,
    ModelInferenceCredentialCreateRequest,
    register_inference_credential,
)
from omnimarket.routing import byok_model_discovery as discovery
from omnimarket.routing.byok_provider_backends import (
    BYOK_MODEL_UNRESOLVED,
    ModelByokProviderBackend,
    resolve_byok_provider_backend,
    select_byok_model,
)
from omnimarket.tenant_credential_ref import is_tenant_credential_ref
from tests.chains.local.harness import (
    house_openrouter_rung,
    run_local_delegation,
    use_local_store,
)
from tests.chains.local.test_chain_l6_typed_credential_refusals_omn18698 import (
    _ladder_with_every_successor,
)

pytestmark = pytest.mark.unit

KEY = (
    "AIza" + "syntheticOMN20157" * 3
)  # onex-allow-test-fixture OMN-20157 reason="synthetic credential shape, asserted absent from outputs"
REF = f"cred_acme_gemini_{'a' * 32}"
OLD = "gemini-2.5-flash-lite"
NEW = "gemini-3.5-flash-lite"
MODEL_SENTENCE = (
    "This model models/gemini-2.5-flash-lite is no longer available to new users. "
    "Please update your code to use models/gemini-3.5-flash-lite."
)
BILLING_SENTENCE = "Your project has no prepaid credits. Add credits in Google AI Studio billing to continue."
SUCCESSOR_URL = "https://successor.invalid/v1/chat/completions"


def _row(provider: str = "gemini") -> ModelByokProviderBackend:
    row = resolve_byok_provider_backend(provider)
    assert row is not None
    return row


def _safe(*values: object) -> None:
    for value in values:
        assert KEY not in str(value)


@pytest.fixture(autouse=True)
def _isolated_effects(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> Iterator[None]:
    # No health/served-model socket probes, and no reads of the operator's store.
    use_local_store(monkeypatch, tmp_path)
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(effect, "_is_endpoint_healthy", lambda _: True)
    monkeypatch.setattr(effect, "_get_served_model_ids", lambda _: None)
    yield
    _safe(caplog.text)


@pytest.mark.parametrize(
    (
        "provider",
        "listed",
        "winner",
        "runner_up",
        "fallback",
        "fallback_winner",
        "unpreferred",
    ),
    [
        (
            "gemini",
            [
                OLD,
                "gemini-3.1-flash-lite",
                NEW,
                "gemini-3.8-flash",
                "gemini-2.5-flash-lite-preview-09-2025",
                "gemini-flash-lite-latest",
                "gemini-3.5-pro",
            ],
            NEW,
            "gemini-3.1-flash-lite",
            ["gemini-2.5-flash", "gemini-3.8-flash"],
            "gemini-3.8-flash",
            ["gemini-3.5-pro"],
        ),
        (
            "glm",
            [
                "glm-4.5-flash",
                "glm-5.1-flash",
                "glm-5.3-flash",
                "glm-5.8-flashx",
                "glm-5.9-flash-preview",
                "glm-flash-latest",
                "glm-5.3-pro",
            ],
            "glm-5.3-flash",
            "glm-5.1-flash",
            ["glm-4.7-flashx", "glm-5.8-flashx"],
            "glm-5.8-flashx",
            ["glm-5.3-pro"],
        ),
        (
            "openrouter",
            [
                "nvidia/nemotron-2-ultra-550b-a55b:free",
                "nvidia/nemotron-3-ultra-550b-a55b:free",
                "nvidia/nemotron-4-ultra-550b-a55b:free",
                "qwen/qwen9-coder:free",
                "nvidia/nemotron-5-ultra-550b-a55b",
                "other/pro",
            ],
            "nvidia/nemotron-4-ultra-550b-a55b:free",
            "nvidia/nemotron-3-ultra-550b-a55b:free",
            ["qwen/qwen3-coder:free", "qwen/qwen4-coder:free"],
            "qwen/qwen4-coder:free",
            ["other/pro"],
        ),
    ],
)
def test_shipped_preferences_choose_family_then_newest_generation(
    provider: str,
    listed: list[str],
    winner: str,
    runner_up: str,
    fallback: list[str],
    fallback_winner: str,
    unpreferred: list[str],
) -> None:
    row = _row(provider)
    if provider == "glm":
        assert row.plan == "general_api"
    assert select_byok_model(row, listed) == winner
    assert select_byok_model(row, list(reversed(listed))) == winner
    assert select_byok_model(row, listed, exclude=(winner,)) == runner_up
    assert select_byok_model(row, fallback) == fallback_winner
    assert select_byok_model(row, unpreferred) is None


def _error(status: int, sentence: str, *, google: bool = True) -> Any:
    body = {
        "error": {
            "code": status,
            "message": sentence,
            "status": "NOT_FOUND" if status == 404 else "PAYMENT_REQUIRED",
        }
    }
    return [body] if google else body


class FakeModels:
    def __init__(
        self, body: Any = None, *, status: int = 200, connection_error: bool = False
    ) -> None:
        self.body = (
            body
            if body is not None
            else {"models": [{"name": f"models/{OLD}"}, {"name": f"models/{NEW}"}]}
        )
        self.status = status
        self.connection_error = connection_error
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        request = httpx.Request("GET", kwargs["url"])
        if self.connection_error:
            raise httpx.ConnectError(
                f"fake connection failed with {KEY}", request=request
            )
        response = httpx.Response(self.status, json=self.body, request=request)
        response.raise_for_status()
        return response.json()


@pytest.mark.parametrize("google", [True, False])
def test_model_discovery_reads_exact_declared_url_with_bearer(google: bool) -> None:
    body = (
        {"models": [{"name": f"models/{NEW}"}]} if google else {"data": [{"id": NEW}]}
    )
    get = FakeModels(body)
    result = discovery.discover_byok_model_sync(_row(), KEY, get=get)
    assert (result.outcome, result.model, result.listed_count) == ("resolved", NEW, 1)
    assert len(get.calls) == 1
    assert get.calls[0]["url"] == _row().models_url
    assert get.calls[0]["extra_headers"] == {"Authorization": f"Bearer {KEY}"}
    _safe(result.model_dump(), discovery.describe_discovery_refusal(result))


@pytest.mark.parametrize(
    ("status", "outcome"), [(401, "rejected"), (402, "billing"), (503, "inconclusive")]
)
def test_discovery_statuses_preserve_scrubbed_provider_message(
    status: int, outcome: str
) -> None:
    get = FakeModels(_error(status, f"{BILLING_SENTENCE} Key {KEY}"), status=status)
    result = discovery.discover_byok_model_sync(_row(), KEY, get=get)
    assert result.outcome == outcome
    assert result.http_status == status
    assert BILLING_SENTENCE in (result.provider_message or "")
    assert "[redacted]" in (result.provider_message or "")
    _safe(result.model_dump(), discovery.describe_discovery_refusal(result))


def test_discovery_connection_error_is_inconclusive_and_logs_no_exception_text() -> (
    None
):
    result = discovery.discover_byok_model_sync(
        _row(), KEY, get=FakeModels(connection_error=True)
    )
    assert result.outcome == "inconclusive"
    _safe(result.model_dump())


def test_discovery_unpreferred_list_is_no_match() -> None:
    result = discovery.discover_byok_model_sync(
        _row(), KEY, get=FakeModels({"data": [{"id": "gemini-3.5-pro"}]})
    )
    assert (result.outcome, result.model, result.listed_count) == ("no_match", None, 1)
    _safe(result.model_dump(), discovery.describe_discovery_refusal(result))


@pytest.mark.parametrize(
    ("status", "detail", "expected"),
    [
        (402, "", Failure.PROVIDER_BILLING),
        (400, "Your prepaid credits are depleted", Failure.PROVIDER_BILLING),
        (403, "Your prepaid credits are depleted", Failure.PROVIDER_BILLING),
        (429, "check your plan and billing details", Failure.RATE_LIMITED),
        (404, "", Failure.PROVIDER_MODEL_NOT_FOUND),
        (401, "", Failure.PROVIDER_AUTH_FAILED),
        (403, "", Failure.PROVIDER_AUTH_FAILED),
        (
            400,
            "API key not valid. Please pass a valid API key.",
            Failure.PROVIDER_AUTH_FAILED,
        ),
        (500, "", None),
        (503, "", None),
    ],
)
def test_provider_status_classification(
    status: int, detail: str, expected: Failure | None
) -> None:
    assert failure_class_for_status(status, detail) is expected


def test_refusal_preserves_sentence_and_scrubs_google_key_shape() -> None:
    message = describe_provider_refusal(
        Failure.PROVIDER_BILLING,
        status_code=402,
        provider_text=json.dumps(_error(402, f"{BILLING_SENTENCE} Key {KEY}")),
        model_id=NEW,
    )
    assert BILLING_SENTENCE in message
    assert "PROVIDER_BILLING" in message
    assert "[redacted]" in message
    _safe(message)


class FakeProvider:
    """One prepared status per POST, recording route and model, never the key."""

    def __init__(self, statuses: list[int]) -> None:
        self.statuses = iter(statuses)
        self.calls: list[tuple[str, str]] = []

    def response(
        self, url: str, payload: dict[str, Any], headers: dict[str, str]
    ) -> httpx.Response:
        self.calls.append((url, payload["model"]))
        if url == SUCCESSOR_URL:
            # A house successor rung on the local ladder (the transport-failure
            # climb that predates this change); it never sees the customer key.
            assert headers.get("Authorization") != f"Bearer {KEY}"
            return httpx.Response(
                503,
                request=httpx.Request("POST", url),
                json=_error(503, "Service temporarily unavailable"),
            )
        assert headers.get("Authorization") == f"Bearer {KEY}"
        status = next(self.statuses)  # An unexpected retry fails the test.
        if status == 200:
            body = {
                "choices": [
                    {
                        "message": {"content": "### ANSWER\nprint('ok')\n"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }
        else:
            sentence = (
                MODEL_SENTENCE
                if status == 404
                else BILLING_SENTENCE
                if status == 402
                else "Incorrect API key provided."
                if status == 401
                else "Service temporarily unavailable"
            )
            body = _error(status, sentence)
        return httpx.Response(status, request=httpx.Request("POST", url), json=body)

    def post(
        self,
        *,
        endpoint_url: str,
        payload: dict[str, Any],
        extra_headers: dict[str, str],
        **_: Any,
    ) -> transport.ModelTransportResponse:
        response = self.response(endpoint_url, payload, extra_headers)
        response.raise_for_status()
        return transport.ModelTransportResponse(
            status_code=200, json_body=response.json(), latency_ms=1
        )


def _bind_effect(
    monkeypatch: pytest.MonkeyPatch,
    statuses: list[int],
    *,
    models: FakeModels | None = None,
) -> tuple[FakeProvider, FakeModels]:
    provider = FakeProvider(statuses)
    get = models or FakeModels()
    monkeypatch.setattr(transport, "post_chat_completion", provider.post)
    monkeypatch.setattr(discovery, "get_models_json", get)
    monkeypatch.setattr(
        effect,
        "resolve_api_key_with_source_loop_safe",
        lambda *_args, **_kwargs: (SecretStr(KEY), EnumSecretSource.LOCAL_STORE),
    )
    return provider, get


def _request(*, model: str = OLD, ref: str = REF) -> ModelLlmDelegationCallRequest:
    return ModelLlmDelegationCallRequest(
        request_id=str(uuid4()),
        correlation_id=str(uuid4()),
        causation_id=str(uuid4()),
        model_id=model,
        endpoint_ref=_row().endpoint_url,
        prompt="say hi",
        prompt_hash="0" * 64,
        timeout_seconds=5,
        secret_ref=ref,
        api_key_env=None,
    )


def test_customer_new_key_404_re_aims_once_and_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert is_tenant_credential_ref(REF)
    provider, get = _bind_effect(monkeypatch, [404, 200])
    result = effect.HandlerLlmDelegationCall().handle(_request())
    assert result.success, result.error_message
    assert provider.calls == [(_row().endpoint_url, OLD), (_row().endpoint_url, NEW)]
    assert [call["url"] for call in get.calls] == [_row().models_url]
    assert result.model_id == NEW
    _safe(result.model_dump())


def test_customer_second_404_is_terminal_with_provider_sentence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, get = _bind_effect(monkeypatch, [404, 404])
    result = effect.HandlerLlmDelegationCall().handle(_request())
    assert not result.success
    assert result.failure_class is Failure.PROVIDER_MODEL_NOT_FOUND
    assert MODEL_SENTENCE in result.error_message
    assert "PROVIDER_MODEL_NOT_FOUND" in result.error_message
    assert provider.calls == [(_row().endpoint_url, OLD), (_row().endpoint_url, NEW)]
    assert len(get.calls) == 1
    _safe(result.model_dump())


def test_customer_billing_is_one_post_with_verbatim_sentence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, get = _bind_effect(monkeypatch, [402])
    result = effect.HandlerLlmDelegationCall().handle(_request())
    assert not result.success
    assert result.failure_class is Failure.PROVIDER_BILLING
    assert BILLING_SENTENCE in result.error_message
    assert "PROVIDER_BILLING" in result.error_message
    assert len(provider.calls) == 1
    assert get.calls == []
    _safe(result.model_dump())


def test_unresolved_customer_model_is_discovered_before_first_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, get = _bind_effect(monkeypatch, [200])
    real_post = provider.post

    def post(**kwargs: Any) -> transport.ModelTransportResponse:
        assert len(get.calls) == 1, "discovery must precede the first POST"
        return real_post(**kwargs)

    monkeypatch.setattr(transport, "post_chat_completion", post)
    result = effect.HandlerLlmDelegationCall().handle(
        _request(model=BYOK_MODEL_UNRESOLVED)
    )
    assert result.success, result.error_message
    assert provider.calls == [(_row().endpoint_url, NEW)]
    _safe(result.model_dump())


def test_house_404_never_re_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    assert not is_tenant_credential_ref("llm.gemini.api_key")
    provider, get = _bind_effect(monkeypatch, [404])
    result = effect.HandlerLlmDelegationCall().handle(
        _request(ref="llm.gemini.api_key")
    )
    assert result.failure_class is Failure.PROVIDER_MODEL_NOT_FOUND
    assert provider.calls == [(_row().endpoint_url, OLD)]
    assert get.calls == []
    _safe(result.model_dump())


@pytest.mark.parametrize(
    ("statuses", "failure", "expected_posts"),
    [
        ([402], Failure.PROVIDER_BILLING, 1),
        ([401], Failure.PROVIDER_AUTH_FAILED, 1),
        ([404, 404], Failure.PROVIDER_MODEL_NOT_FOUND, 2),
    ],
)
async def test_local_dispatch_terminalises_customer_route_without_house_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    statuses: list[int],
    failure: Failure,
    expected_posts: int,
) -> None:
    row = _row("openrouter")
    old = "nvidia/nemotron-3-ultra-550b-a55b:free"
    new = "nvidia/nemotron-4-ultra-550b-a55b:free"
    provider, get = _bind_effect(
        monkeypatch, statuses, models=FakeModels({"data": [{"id": old}, {"id": new}]})
    )
    db_path = use_local_store(monkeypatch, tmp_path)
    # Leave real store resolution active for dispatch; no fake can supply a house key.
    from omnimarket.inference.secret_store_resolver import (
        resolve_api_key_with_source_loop_safe,
    )

    monkeypatch.setattr(
        effect,
        "resolve_api_key_with_source_loop_safe",
        resolve_api_key_with_source_loop_safe,
    )
    credentials.register_local_byok_credential(
        "openrouter", KEY, model=old, db_path=db_path
    )
    _ladder_with_every_successor(
        monkeypatch,
        first_rung=house_openrouter_rung(monkeypatch),
        next_rung_url=SUCCESSOR_URL,
    )
    started = time.monotonic()
    response = await run_local_delegation(
        prompt="write a function that parses a semver string",
        db_path=db_path,
        correlation_id=uuid4(),
    )
    elapsed = time.monotonic() - started
    assert response.status == "failed", response.model_dump()
    assert response.attempts[-1].failure_class == failure.value
    assert len(provider.calls) == expected_posts
    assert all(url == row.endpoint_url for url, _ in provider.calls)
    assert all(attempt.backend_id == row.backend_id for attempt in response.attempts)
    assert response.escalation_count == 0
    if failure is Failure.PROVIDER_BILLING:
        assert elapsed < 5
        assert BILLING_SENTENCE in (response.error_message or "")
        assert "PROVIDER_BILLING" in (response.error_message or "")
        assert get.calls == []
    elif failure is Failure.PROVIDER_MODEL_NOT_FOUND:
        assert len(get.calls) == 1
        assert [model for _, model in provider.calls] == [old, new]
        assert MODEL_SENTENCE in (response.error_message or "")
    _safe(response.model_dump())


async def test_local_dispatch_retries_a_customer_5xx_on_its_own_backend_within_budget(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A real transport failure (503) keeps its retry, bounded and on one backend.

    The positive control for the typed refusals above: a 503 is re-issued to the
    customer's own route up to the catalogue's ``max_retries``, and only then
    does the ladder move on as it did before this change. Typed refusals never
    reach that retry at all.
    """
    row = _row("openrouter")
    old = "nvidia/nemotron-3-ultra-550b-a55b:free"
    budget = row.max_retries
    provider, _get = _bind_effect(
        monkeypatch,
        [503] * (budget + 1),
        models=FakeModels({"data": [{"id": old}]}),
    )
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
        "openrouter", KEY, model=old, db_path=db_path
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
    assert response.status == "failed", response.model_dump()
    customer_calls = [call for call in provider.calls if call[0] == row.endpoint_url]
    assert budget > 0
    assert len(customer_calls) == budget + 1
    customer_attempts = [
        attempt for attempt in response.attempts if attempt.backend_id == row.backend_id
    ]
    assert len(customer_attempts) == budget + 1
    assert all(
        attempt.failure_class == Failure.MODEL_UNAVAILABLE.value
        for attempt in customer_attempts
    )
    _safe(response.model_dump())


class FakeClient:
    def __init__(self, provider: FakeProvider) -> None:
        self.provider = provider

    def __call__(self, **_: Any) -> FakeClient:
        return self

    def __enter__(self) -> FakeClient:
        return self

    def __exit__(self, *_: Any) -> None:
        return None

    def post(
        self, url: str, *, json: dict[str, Any], headers: dict[str, str], **_: Any
    ) -> httpx.Response:
        return self.provider.response(url, json, headers)


def _bind_bus(
    monkeypatch: pytest.MonkeyPatch, statuses: list[int]
) -> tuple[FakeProvider, FakeModels]:
    provider = FakeProvider(statuses)
    get = FakeModels()
    monkeypatch.setattr(bus_effect.httpx, "Client", FakeClient(provider))
    monkeypatch.setattr(bus_effect, "_resolve_api_key", lambda _: KEY)
    monkeypatch.setattr(discovery, "get_models_json", get)
    return provider, get


def _intent() -> ModelInferenceIntent:
    return ModelInferenceIntent(
        base_url=_row().endpoint_url,
        model=OLD,
        system_prompt="s",
        prompt="p",
        max_tokens=64,
        correlation_id=uuid4(),
        tenant_id="acme",
        api_key_ref=REF,
    )


def test_bus_billing_raises_typed_refusal_and_text_classifier_agrees(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, get = _bind_bus(monkeypatch, [402])
    with pytest.raises(bus_effect.ProviderRefusalError) as raised:
        bus_effect.HandlerInferenceIntent()._call_llm_on_resolved_model(
            _intent(),
            str(uuid4()),
            api_key=KEY,
            credential_source=bus_effect.EnumCredentialSource.CUSTOMER_KEY,
        )
    assert raised.value.failure_class is Failure.PROVIDER_BILLING
    assert BILLING_SENTENCE in str(raised.value)
    assert _inference_error_failure_class(str(raised.value)) is Failure.PROVIDER_BILLING
    assert len(provider.calls) == 1
    assert get.calls == []
    _safe(raised.value)


def test_bus_billing_public_response_preserves_typed_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, _ = _bind_bus(monkeypatch, [402])
    result = bus_effect.HandlerInferenceIntent().handle(_intent())
    assert BILLING_SENTENCE in result.error_message
    assert (
        _inference_error_failure_class(result.error_message) is Failure.PROVIDER_BILLING
    )
    assert len(provider.calls) == 1
    _safe(result.model_dump())


@pytest.mark.parametrize("second_status", [200, 404])
def test_bus_customer_404_re_aims_only_once(
    monkeypatch: pytest.MonkeyPatch, second_status: int
) -> None:
    provider, get = _bind_bus(monkeypatch, [404, second_status])
    result = bus_effect.HandlerInferenceIntent().handle(_intent())
    assert provider.calls == [(_row().endpoint_url, OLD), (_row().endpoint_url, NEW)]
    assert [call["url"] for call in get.calls] == [_row().models_url]
    if second_status == 200:
        assert result.error_message == ""
        assert result.content
        assert result.model_used == NEW
    else:
        assert MODEL_SENTENCE in result.error_message
        assert (
            _inference_error_failure_class(result.error_message)
            is Failure.PROVIDER_MODEL_NOT_FOUND
        )
    _safe(result.model_dump())


@pytest.mark.parametrize("billing", [True, False])
def test_cli_discovery_decides_before_storage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, billing: bool
) -> None:
    db_path = use_local_store(monkeypatch, tmp_path)
    result = discovery.ModelByokModelDiscovery(
        provider="gemini",
        plan="ai_studio",
        outcome="billing" if billing else "resolved",
        model=None if billing else NEW,
        http_status=402 if billing else 200,
        provider_message=BILLING_SENTENCE if billing else None,
    )
    calls: list[str] = []

    def discover(
        row: ModelByokProviderBackend, key: str
    ) -> discovery.ModelByokModelDiscovery:
        assert key == KEY
        calls.append(row.provider)
        return result

    monkeypatch.setattr(cli_secret, "discover_byok_model_sync", discover)
    command = CliRunner().invoke(
        cli_secret.secret_group,
        ["set", "llm.gemini.api_key"],
        input=f"{KEY}\n",
        catch_exceptions=False,
    )
    assert calls == ["gemini"]
    if billing:
        assert command.exit_code != 0
        assert BILLING_SENTENCE in command.output
        assert "PROVIDER_BILLING" in command.output
        assert "Nothing was stored." in command.output
        assert (
            asyncio.run(credentials.LocalByokCredentialStore(db_path).list_keys()) == []
        )
        assert (
            credentials.resolve_local_byok_credential_ref("gemini", db_path=db_path)
            is None
        )
    else:
        assert command.exit_code == 0, command.output
        assert f"model: {NEW}" in command.output
        assert (
            credentials.resolve_local_byok_credential_model("gemini", db_path=db_path)
            == NEW
        )
        ref = credentials.resolve_local_byok_credential_ref("gemini", db_path=db_path)
        assert ref is not None
        assert (
            asyncio.run(credentials.LocalByokCredentialStore(db_path).get_secret(ref))
            == KEY
        )
    _safe(command.output, command.exception)


@pytest.mark.parametrize("billing", [True, False])
async def test_hosted_discovery_model_rides_event_or_refuses_before_any_write(
    billing: bool,
) -> None:
    store = AsyncMock()
    store.set_secret.return_value = True
    bus = AsyncMock(spec=EventBusKafka)
    discoverer = AsyncMock(
        return_value=discovery.ModelByokModelDiscovery(
            provider="gemini",
            plan="ai_studio",
            outcome="billing" if billing else "resolved",
            model=None if billing else NEW,
            http_status=402 if billing else 200,
            provider_message=BILLING_SENTENCE if billing else None,
        )
    )
    request = ModelInferenceCredentialCreateRequest(
        name="test-key", provider="gemini", key_value=KEY
    )
    if billing:
        with pytest.raises(CredentialKeyRefusedError) as raised:
            await register_inference_credential(
                request,
                tenant_id="acme",
                secret_store=store,
                event_bus=bus,
                model_discoverer=discoverer,
            )
        assert BILLING_SENTENCE in str(raised.value)
        assert "PROVIDER_BILLING" in str(raised.value)
        store.set_secret.assert_not_awaited()
        bus.publish_envelope.assert_not_awaited()
        _safe(raised.value)
    else:
        result = await register_inference_credential(
            request,
            tenant_id="acme",
            secret_store=store,
            event_bus=bus,
            model_discoverer=discoverer,
        )
        assert result.model == NEW
        store.set_secret.assert_awaited_once()
        bus.publish_envelope.assert_awaited_once()
        event = bus.publish_envelope.await_args.args[0].payload
        assert event.metadata == {"model": NEW}
        _safe(event.model_dump(), result.model_dump())
    discoverer.assert_awaited_once()
    assert discoverer.await_args.args[0] == _row()
    assert discoverer.await_args.args[1].get_secret_value() == KEY
