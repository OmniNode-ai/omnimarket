# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20844: a model the customer chose is never swapped for a catalogue pick.

The OMN-20157/OMN-19205/OMN-20555 re-aim moves a failed customer call to the
best model the catalogue prefers on the key's own list, and every OpenRouter
preference is a ``:free`` model. That is right for a model the catalogue picked
and wrong for one the customer chose: a paying customer's 429 would be answered
by a free model. On a row that declares ``customer_chooses_model``:

* a call on a model the catalogue does not prefer is made once; a 404 or a
  throttle stands as the provider's answer and the stored model is unchanged;
* a credential with no model (the unresolved marker) is refused with
  ``BYOK_MODEL_NOT_CHOSEN`` before any call, instead of resolving a ``:free``
  model for the customer.

A model the catalogue prefers (a ``:free`` slug the customer may still choose)
keeps the existing one-shot re-aim, pinned by the OMN-19205/OMN-20555 tests.
Fakes only, no network.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from omnibase_core.models.delegation.wire import ModelInferenceIntent

from omnimarket.nodes.node_llm_delegation_call_effect.handlers import (
    handler_inference_intent as bus_effect,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers import (
    handler_llm_delegation_call as effect,
)
from omnimarket.routing import byok_model_discovery as discovery
from omnimarket.routing.byok_provider_backends import BYOK_MODEL_UNRESOLVED
from tests.test_omn19205_c29_byok_throttle_re_resolve import (
    GEMMA,
    KEY,
    NEMOTRON,
    REF,
    _bind,
    _openrouter,
    _request,
)
from tests.test_omn20157_byok_model_discovery_and_typed_refusals import (
    KEY as PROVIDER_FAKE_KEY,
)
from tests.test_omn20157_byok_model_discovery_and_typed_refusals import (
    FakeClient,
    FakeModels,
    FakeProvider,
    _isolated_effects,
)

pytestmark = pytest.mark.unit

_SHARED_AUTOUSE_FIXTURE = _isolated_effects

PAID = "openai/gpt-5-nano"


def _chosen(model: str = PAID) -> Any:
    return _request().model_copy(update={"model_id": model})


@pytest.mark.parametrize("status", [429, 404], ids=["throttle", "not-found"])
def test_a_chosen_paid_model_is_called_once_and_never_swapped(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    provider, models = _bind(monkeypatch, [status], [PAID, GEMMA, NEMOTRON])
    recorded: list[tuple[str, str]] = []
    monkeypatch.setattr(
        effect,
        "record_local_byok_model",
        lambda ref, model: recorded.append((ref, model)),
    )
    result = effect.HandlerLlmDelegationCall().__call__(_chosen())

    assert not result.success
    assert [call[1] for call in provider.calls] == [PAID]
    assert models.calls == []
    assert recorded == []
    assert KEY not in str(result.model_dump())


def test_an_unresolved_openrouter_credential_is_refused_before_any_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, models = _bind(monkeypatch, [], [GEMMA, NEMOTRON])
    result = effect.HandlerLlmDelegationCall().__call__(_chosen(BYOK_MODEL_UNRESOLVED))

    assert not result.success
    assert "BYOK_MODEL_NOT_CHOSEN" in (result.error_message or "")
    assert "--model" in (result.error_message or "")
    assert provider.calls == []
    assert models.calls == []


def test_a_catalogue_preferred_free_model_still_re_aims_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, _ = _bind(monkeypatch, [429, 200], [GEMMA, NEMOTRON])
    result = effect.HandlerLlmDelegationCall().__call__(_chosen(GEMMA))

    assert result.success, result.error_message
    assert [call[1] for call in provider.calls] == [GEMMA, NEMOTRON]


def _bus_intent(model: str) -> ModelInferenceIntent:
    return ModelInferenceIntent(
        base_url=_openrouter().endpoint_url,
        model=model,
        system_prompt="s",
        prompt="p",
        max_tokens=64,
        correlation_id=uuid4(),
        tenant_id="acme",
        api_key_ref=REF,
    )


def _bus_bind(
    monkeypatch: pytest.MonkeyPatch, statuses: list[int]
) -> tuple[FakeProvider, FakeModels]:
    provider = FakeProvider(statuses)
    models = FakeModels({"data": [{"id": m} for m in (PAID, GEMMA, NEMOTRON)]})
    monkeypatch.setattr(f"{bus_effect.__name__}.httpx.Client", FakeClient(provider))
    monkeypatch.setattr(bus_effect, "_resolve_api_key", lambda _: PROVIDER_FAKE_KEY)
    monkeypatch.setattr(discovery, "get_models_json", models)
    return provider, models


@pytest.mark.parametrize("status", [429, 404], ids=["throttle", "not-found"])
def test_the_bus_effect_never_swaps_a_chosen_paid_model(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    provider, models = _bus_bind(monkeypatch, [status])
    result = bus_effect.HandlerInferenceIntent().handle(_bus_intent(PAID))

    assert result.error_message
    assert [call[1] for call in provider.calls] == [PAID]
    assert models.calls == []


def test_the_bus_effect_refuses_an_unresolved_openrouter_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, models = _bus_bind(monkeypatch, [])
    result = bus_effect.HandlerInferenceIntent().handle(
        _bus_intent(BYOK_MODEL_UNRESOLVED)
    )

    assert "BYOK_MODEL_NOT_CHOSEN" in result.error_message
    assert provider.calls == []
    assert models.calls == []
