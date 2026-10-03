# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19205 follow-up: the one-shot throttle switch leaves the throttled family.

On 2026-10-02 the C29 customer key resolved ``google/gemma-4-31b-it:free``. It
answered 429 "temporarily rate-limited upstream". The OMN-19205 one-shot switch
re-resolved from the key's own list excluding only that slug, and so picked its
sibling ``google/gemma-4-26b-a4b-it:free``, served by the same upstream (Google
AI Studio) and throttled at the same moment. The delegation failed although
``nvidia/nemotron-3-super-120b-a12b:free`` was answering.

The recorded provider answers are in
``tests/fixtures/omn19205/openrouter_free_tier_2026-10-02.json``: both gemma
slugs 429, nemotron-ultra an in-body 503, nemotron-super a 200 with content.
The switch now excludes every listed model of the failed model's preference
family, and the catalogue prefers nemotron-super over nemotron-ultra. Fakes
only, no network.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnimarket.nodes.node_llm_delegation_call_effect.handlers import (
    handler_llm_delegation_call as effect,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers import transport
from omnimarket.routing.byok_provider_backends import (
    resolve_byok_provider_backend,
    select_byok_model,
)
from tests.test_omn19205_c29_byok_throttle_re_resolve import (
    KEY,
    SUCCESSOR_URL,
    _bind,
    _openrouter,
    _request,
)
from tests.test_omn20157_byok_model_discovery_and_typed_refusals import (
    _isolated_effects,
)

pytestmark = pytest.mark.unit

_SHARED_AUTOUSE_FIXTURE = _isolated_effects

FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "omn19205"
    / "openrouter_free_tier_2026-10-02.json"
)
GEMMA = "google/gemma-4-31b-it:free"
GEMMA_MOE = "google/gemma-4-26b-a4b-it:free"
NEMOTRON_ULTRA = "nvidia/nemotron-3-ultra-550b-a55b:free"
NEMOTRON_SUPER = "nvidia/nemotron-3-super-120b-a12b:free"
LISTED = [GEMMA, GEMMA_MOE, NEMOTRON_ULTRA, NEMOTRON_SUPER]


def _recorded() -> dict[str, dict[str, Any]]:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return {entry["model"]: entry for entry in data["responses"]}


class RecordedOpenRouter:
    """Answers each POST with the response recorded for the requested model."""

    def __init__(self) -> None:
        self.recorded = _recorded()
        self.calls: list[str] = []

    def post(
        self,
        *,
        endpoint_url: str,
        payload: dict[str, Any],
        extra_headers: dict[str, str],
        **_: Any,
    ) -> transport.ModelTransportResponse:
        assert endpoint_url != SUCCESSOR_URL, "the switch must not change backend"
        assert extra_headers.get("Authorization") == f"Bearer {KEY}"
        model = payload["model"]
        self.calls.append(model)
        entry = self.recorded[model]
        if entry["http_status"] != 200:
            response = httpx.Response(
                entry["http_status"],
                request=httpx.Request("POST", endpoint_url),
                json=entry["body"],
            )
            response.raise_for_status()
        return transport.ModelTransportResponse(
            status_code=200, json_body=entry["body"], latency_ms=1
        )


def test_the_fixture_is_the_recorded_shape() -> None:
    recorded = _recorded()
    assert recorded[GEMMA]["http_status"] == 429
    assert recorded[GEMMA_MOE]["http_status"] == 429
    assert recorded[NEMOTRON_ULTRA]["body"]["error"]["code"] == 503
    assert recorded[NEMOTRON_SUPER]["body"]["choices"][0]["message"]["content"]
    assert KEY not in FIXTURE.read_text(encoding="utf-8")


def test_a_throttled_family_is_left_whole_and_a_live_family_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RED on dev: the switch picked the throttled sibling and failed."""
    _bind(monkeypatch, [], LISTED)
    provider = RecordedOpenRouter()
    monkeypatch.setattr(transport, "post_chat_completion", provider.post)
    result = effect.HandlerLlmDelegationCall().__call__(_request())
    assert result.success, result.error_message
    assert provider.calls == [GEMMA, NEMOTRON_SUPER]
    assert result.served_model_id == NEMOTRON_SUPER
    assert [a.model_id for a in result.earlier_model_attempts] == [GEMMA]
    assert KEY not in str(result.model_dump())


def test_the_switch_is_still_one_shot_when_the_next_family_is_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A key that lists no nemotron-super: gemma 429, then ultra's in-body 503, stop."""
    _bind(monkeypatch, [], [GEMMA, GEMMA_MOE, NEMOTRON_ULTRA])
    provider = RecordedOpenRouter()
    monkeypatch.setattr(transport, "post_chat_completion", provider.post)
    result = effect.HandlerLlmDelegationCall().__call__(_request())
    assert not result.success
    assert provider.calls == [GEMMA, NEMOTRON_ULTRA]
    assert GEMMA in result.error_message
    assert NEMOTRON_ULTRA in result.error_message


def test_a_404_re_resolve_still_excludes_only_the_missing_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 404 is about one id, not an upstream: its sibling stays eligible."""
    provider, _ = _bind(monkeypatch, [404, 200], [GEMMA, GEMMA_MOE, NEMOTRON_SUPER])
    result = effect.HandlerLlmDelegationCall().__call__(_request())
    assert result.success, result.error_message
    assert [call[1] for call in provider.calls] == [GEMMA, GEMMA_MOE]


def test_the_catalogue_prefers_nemotron_super_over_nemotron_ultra() -> None:
    row = resolve_byok_provider_backend("openrouter")
    assert row is not None
    assert select_byok_model(row, LISTED) == GEMMA
    assert (
        select_byok_model(row, LISTED, exclude_families_of=(GEMMA,)) == NEMOTRON_SUPER
    )
    assert select_byok_model(row, [NEMOTRON_ULTRA, NEMOTRON_SUPER]) == NEMOTRON_SUPER
    assert _openrouter().backend_id == row.backend_id
