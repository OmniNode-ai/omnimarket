# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20555: a throttled customer model on the BUS effect leaves its family.

The OMN-19205 switch (a 429, or an aggregator's in-body upstream error, re-aims
the customer route once at the next family on the key's own model list) lived
only on the in-process ``HandlerLlmDelegationCall``. The bus effect
``HandlerInferenceIntent``, which every deployed-lane delegation runs through,
re-aimed only on a 404. So every byok-openrouter delegation on the dev lane hit
``google/gemma-4-31b-it:free``'s 429 and stopped there (2026-10-04/05: tenant
a630ba90, 395 runs, 0 answers, while nemotron-super was answering).

The provider answers are the ones recorded for OMN-19205 in
``tests/fixtures/omn19205/openrouter_free_tier_2026-10-02.json``. Fakes only.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import httpx
import pytest
from omnibase_core.models.delegation.wire import ModelInferenceIntent

from omnimarket.nodes.node_llm_delegation_call_effect.handlers import (
    handler_inference_intent as bus_effect,
)
from omnimarket.routing import byok_model_discovery as discovery
from tests.test_omn19205_c29_byok_throttle_re_resolve import KEY, REF, _openrouter
from tests.test_omn19205_c29_throttle_crosses_family import (
    GEMMA,
    GEMMA_MOE,
    LISTED,
    NEMOTRON_SUPER,
    NEMOTRON_ULTRA,
    _recorded,
)
from tests.test_omn20157_byok_model_discovery_and_typed_refusals import (
    FakeClient,
    FakeModels,
    FakeProvider,
    _isolated_effects,
)

pytestmark = pytest.mark.unit

_SHARED_AUTOUSE_FIXTURE = _isolated_effects

HOUSE_REF = "llm.openrouter.api_key"


class RecordedBusOpenRouter(FakeProvider):
    """Answers each bus POST with the response recorded for the requested model."""

    def __init__(self) -> None:
        super().__init__([])
        self.recorded = _recorded()
        self.models: list[str] = []

    def response(
        self, url: str, payload: dict[str, Any], headers: dict[str, str]
    ) -> httpx.Response:
        assert url == _openrouter().endpoint_url, "the switch must not change backend"
        assert headers.get("Authorization") == f"Bearer {KEY}"
        model = payload["model"]
        self.models.append(model)
        entry = self.recorded[model]
        return httpx.Response(
            entry["http_status"], request=httpx.Request("POST", url), json=entry["body"]
        )


def _bind(
    monkeypatch: pytest.MonkeyPatch, listed: list[str]
) -> tuple[RecordedBusOpenRouter, FakeModels]:
    provider = RecordedBusOpenRouter()
    models = FakeModels({"data": [{"id": model} for model in listed]})
    monkeypatch.setattr(f"{bus_effect.__name__}.httpx.Client", FakeClient(provider))
    monkeypatch.setattr(bus_effect, "_resolve_api_key", lambda _: KEY)
    monkeypatch.setattr(discovery, "get_models_json", models)
    return provider, models


def _intent(*, ref: str = REF) -> ModelInferenceIntent:
    return ModelInferenceIntent(
        base_url=_openrouter().endpoint_url,
        model=GEMMA,
        system_prompt="s",
        prompt="p",
        max_tokens=64,
        correlation_id=uuid4(),
        tenant_id="acme",
        api_key_ref=ref,
    )


def test_byok_rate_limited_next_catalogue_model_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RED on main: the bus effect returned gemma's 429 after one call."""
    provider, models = _bind(monkeypatch, LISTED)
    result = bus_effect.HandlerInferenceIntent().handle(_intent())
    assert result.error_message == "", result.error_message
    assert result.content
    assert provider.models == [GEMMA, NEMOTRON_SUPER]
    assert result.model_used == NEMOTRON_SUPER
    assert [call["url"] for call in models.calls] == [_openrouter().models_url]
    assert KEY not in str(result.model_dump())


def test_byok_throttle_switch_is_one_shot_when_the_next_family_is_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gemma 429, then nemotron-ultra's in-body 503, then stop: never a third call."""
    provider, _ = _bind(monkeypatch, [GEMMA, GEMMA_MOE, NEMOTRON_ULTRA])
    result = bus_effect.HandlerInferenceIntent().handle(_intent())
    assert provider.models == [GEMMA, NEMOTRON_ULTRA]
    assert result.error_message
    assert GEMMA in result.error_message
    assert NEMOTRON_ULTRA in result.error_message
    assert KEY not in str(result.model_dump())


def test_byok_all_catalogue_models_rate_limited_keeps_the_providers_429(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A key listing only the throttled family has nowhere to go: the 429 stands."""
    provider, models = _bind(monkeypatch, [GEMMA, GEMMA_MOE])
    result = bus_effect.HandlerInferenceIntent().handle(_intent())
    assert provider.models == [GEMMA]
    assert len(models.calls) == 1
    assert "429" in result.error_message
    assert KEY not in str(result.model_dump())


def test_house_route_429_never_re_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    provider, models = _bind(monkeypatch, LISTED)
    result = bus_effect.HandlerInferenceIntent().handle(_intent(ref=HOUSE_REF))
    assert provider.models == [GEMMA]
    assert models.calls == []
    assert "429" in result.error_message
