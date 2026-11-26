# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Every detail a provider's own list-models answer states reaches the model table
(OMN-26688, operator 2026-11-26: "live fetch when fetch model all the details?
same for all others providers?", "cover all edge cases also").

Each entry shape is the provider's own docs, read 2026-11-26:
  - Anthropic GET /v1/models (platform.claude.com/docs/en/api/models-list):
    display_name, max_input_tokens (the input context window), max_tokens (the
    largest max_tokens), capabilities.{image_input, pdf_input,
    structured_outputs, thinking}.supported, each nullable.
  - Reflection GET /openai/v1/models (developers.reflection.ai api-reference/
    models/list-models): top-level input_modalities ("text", "image"), and a
    reasoning object when the model reasons.
  - Google models.list (aistudio.google.com/api-reference/models): displayName,
    inputTokenLimit, outputTokenLimit, thinking (boolean).
  - OpenAI GET /v1/models: id, created, object, owned_by only, so nothing more
    is shown for it.

Failure modes:
  D1  a detail the provider's answer states shows as "-".
  D2  a null, false or malformed detail is shown as a value or a capability.
  D3  an OpenAI entry, which states nothing more, gains invented details.
  D4  a provider is listed through anything but its own native list, with
      its own key header (operator 2026-11-26: "each providers should have
      theire own endpoint and api keys and live fetch details"): Gemini
      through Google's OpenAI-compatible shim, which states ids only, or
      with its key in the URL.
  D5  only a provider's first page is read: Google sends 50 models a page
      unless asked for up to 1000 and continues with nextPageToken
      (aistudio.google.com/api-reference/models/list-models); Anthropic sends
      20 unless asked for up to 1000 and continues with has_more and last_id
      as after_id (platform.claude.com/docs/en/api/models-list).
  D6  a page that repeats its token loops for ever; a later page that fails
      is shown as the whole list.
  D7  a Gemini model that cannot chat (no generateContent among its
      supportedGenerationMethods) is offered for chat.
"""

from __future__ import annotations

import pytest

from omnimarket.handlers.model_catalog import EMPTY_INDEX, listed_catalog_entry
from omnimarket.handlers.routing.byok_model_listing import list_byok_models

pytestmark = pytest.mark.unit


def _one(provider: str, entry: dict) -> object:
    listing = list_byok_models(
        provider, "unused-fixture", get=lambda **_: {"data": [entry]}
    )
    assert listing.outcome == "listed", listing.message
    assert len(listing.models) == 1
    return listing.models[0]


def _yes(provider: str, model: object) -> set[str]:
    entry = listed_catalog_entry(provider, model, EMPTY_INDEX, provider_label=provider)
    return {k for k, v in entry.capabilities.items() if v == "yes"}


def _anthropic(**change: object) -> dict:
    entry: dict = {
        "type": "model",
        "id": "claude-sonnet-5-5",
        "display_name": "Claude Sonnet 5.5",
        "created_at": "2026-05-01T00:00:00Z",
        "max_input_tokens": 1000000,
        "max_tokens": 128000,
        "capabilities": {
            "image_input": {"supported": True},
            "pdf_input": {"supported": True},
            "structured_outputs": {"supported": True},
            "thinking": {"supported": True},
            "batch": {"supported": True},
        },
    }
    entry.update(change)
    return entry


def test_anthropic_states_its_name_limits_and_capabilities() -> None:
    """D1."""
    model = _one("anthropic", _anthropic())
    assert model.name == "Claude Sonnet 5.5"
    assert model.context_tokens == 1000000
    assert model.max_output_tokens == 128000
    assert model.input_usd_per_million is None  # its list states no price
    assert _yes("anthropic", model) == {"Vision", "Files", "JSON", "Reason"}


@pytest.mark.parametrize(
    "change",
    [
        {"max_input_tokens": None, "max_tokens": None, "capabilities": None},
        {"max_input_tokens": "1000000", "max_tokens": True, "capabilities": []},
        {"capabilities": {"image_input": {"supported": False}}},
        {"capabilities": {"image_input": True, "thinking": "yes"}},
        {"capabilities": {"image_input": {"supported": "true"}}},
        {"display_name": None},
    ],
)
def test_anthropic_unstated_details_stay_unknown(change: dict) -> None:
    """D2."""
    model = _one("anthropic", _anthropic(**change))
    if "display_name" in change:
        assert model.name == "claude-sonnet-5-5"
        return
    if "max_input_tokens" in change:
        assert model.context_tokens is None
        assert model.max_output_tokens is None
    assert _yes("anthropic", model) == set()


def test_reflection_states_vision_and_reasoning() -> None:
    """D1."""
    model = _one(
        "reflection",
        {
            "id": "Beam-1.0-501B-A23B",
            "object": "model",
            "created": 0,
            "owned_by": "reflection",
            "shutdown_date": None,
            "input_modalities": ["text", "image"],
            "context_length": 1048576,
            "max_output_tokens": 131072,
            "reasoning": {"supported_efforts": ["max"], "mandatory": True},
        },
    )
    assert model.context_tokens == 1048576
    assert _yes("reflection", model) == {"Vision", "Reason"}


@pytest.mark.parametrize(
    "change",
    [
        {"input_modalities": ["text"], "reasoning": None},
        {"input_modalities": "image", "reasoning": "max"},
        {"input_modalities": [1, None]},
    ],
)
def test_reflection_unstated_capabilities_stay_unknown(change: dict) -> None:
    """D2."""
    entry = {"id": "Beam-1.0-501B-A23B", "object": "model", **change}
    assert _yes("reflection", _one("reflection", entry)) == set()


@pytest.mark.parametrize(
    ("thinking", "expected"),
    [(True, {"Reason"}), (False, set()), ("true", set()), (None, set())],
)
def test_gemini_thinking_is_reasoning(thinking: object, expected: set) -> None:
    """D1, D2."""
    model = _one(
        "gemini",
        {
            "name": "models/gemini-4-flash",
            "displayName": "Gemini 4 Flash",
            "inputTokenLimit": 1048576,
            "outputTokenLimit": 65536,
            "thinking": thinking,
        },
    )
    assert model.id == "gemini-4-flash"
    assert model.name == "Gemini 4 Flash"
    assert model.context_tokens == 1048576
    assert model.max_output_tokens == 65536
    assert _yes("gemini", model) == expected


def test_openai_gains_nothing_it_does_not_state() -> None:
    """D3."""
    model = _one(
        "openai",
        {"id": "gpt-6", "object": "model", "created": 1, "owned_by": "openai"},
    )
    assert model.name == "gpt-6"
    assert model.context_tokens is None
    assert model.input_usd_per_million is None
    assert _yes("openai", model) == set()


class _Pages:
    """A list-models endpoint answering page after page, recording each URL."""

    def __init__(self, *pages: object) -> None:
        self.pages = list(pages)
        self.urls: list[str] = []
        self.headers: list[dict] = []

    def __call__(self, *, url: str, extra_headers: dict, **_: object) -> object:
        self.urls.append(url)
        self.headers.append(dict(extra_headers))
        page = self.pages.pop(0) if self.pages else {"data": []}
        if isinstance(page, Exception):
            raise page
        return page


def _gemini(model: str, *methods: str) -> dict:
    return {
        "name": f"models/{model}",
        "displayName": model.title(),
        "inputTokenLimit": 1048576,
        "outputTokenLimit": 65536,
        "supportedGenerationMethods": list(methods),
    }


def test_gemini_lists_natively_with_its_own_key_header() -> None:
    """D4: a fake key sent in x-goog-api-key is read by Google (400
    API_KEY_INVALID; no key, 403), measured 2026-11-26."""
    from urllib.parse import urlparse

    from omnimarket.handlers.routing.byok_provider_backends import (
        resolve_byok_provider_backend,
    )

    row = resolve_byok_provider_backend("gemini")
    assert row is not None
    assert urlparse(row.models_url).path == "/v1beta/models"
    pages = _Pages({"models": [_gemini("gemini-4-flash", "generateContent")]})
    key = "AIza-test-key-0123456789"
    listing = list_byok_models("gemini", key, get=pages)
    assert listing.outcome == "listed"
    assert pages.headers[0].get("x-goog-api-key") == key
    assert "Authorization" not in pages.headers[0]
    assert key not in pages.urls[0]
    assert "pageSize=1000" in pages.urls[0]


def test_gemini_reads_every_page_and_only_chat_models() -> None:
    """D5, D7."""
    pages = _Pages(
        {
            "models": [
                _gemini("gemini-4-flash", "generateContent", "countTokens"),
                _gemini("text-embedding-5", "embedContent"),
            ],
            "nextPageToken": "page-2",
        },
        {"models": [_gemini("gemini-4-pro", "generateContent"), _gemini("no-methods")]},
    )
    listing = list_byok_models("gemini", "AIza-test-key", get=pages)
    assert listing.outcome == "listed"
    assert [m.id for m in listing.models] == [
        "gemini-4-flash",
        "gemini-4-pro",
        "no-methods",
    ]
    assert listing.models[0].context_tokens == 1048576
    assert len(pages.urls) == 2
    assert "pageToken=page-2" in pages.urls[1]


def test_anthropic_reads_every_page() -> None:
    """D5."""
    first = {
        "data": [{"id": f"claude-{n}"} for n in range(3)],
        "has_more": True,
        "last_id": "claude-2",
    }
    second = {"data": [{"id": "claude-9"}], "has_more": False, "last_id": "claude-9"}
    pages = _Pages(first, second)
    listing = list_byok_models("anthropic", "sk-ant-test", get=pages)
    assert [m.id for m in listing.models] == [
        "claude-0",
        "claude-1",
        "claude-2",
        "claude-9",
    ]
    assert "limit=1000" in pages.urls[0]
    assert "after_id=claude-2" in pages.urls[1]
    assert pages.headers[0].get("x-api-key") == "sk-ant-test"


def test_a_repeated_page_token_stops() -> None:
    """D6: the same token again ends the read with what was listed."""
    page = {
        "models": [_gemini("gemini-4-flash", "generateContent")],
        "nextPageToken": "same",
    }
    pages = _Pages(page, page, page, page)
    listing = list_byok_models("gemini", "AIza-test-key", get=pages)
    assert listing.outcome == "listed"
    assert [m.id for m in listing.models] == ["gemini-4-flash"]
    assert len(pages.urls) == 2


def test_endless_pages_stop_at_the_cap() -> None:
    """D6: a provider that always says there is more is read a bounded number
    of times."""
    from omnimarket.handlers.routing import byok_model_listing

    def endless(*, url: str, **_: object) -> dict:
        n = len(seen)
        seen.append(url)
        return {"data": [{"id": f"m-{n}"}], "has_more": True, "last_id": f"m-{n}"}

    seen: list[str] = []
    listing = list_byok_models("anthropic", "sk-ant-test", get=endless)
    assert len(seen) == byok_model_listing.MAX_LIST_PAGES
    assert listing.outcome == "inconclusive"
    assert listing.models == ()


def test_a_later_page_that_fails_is_not_the_whole_list() -> None:
    """D6."""
    import httpx

    request = httpx.Request("GET", "https://api.anthropic.com/v1/models")
    failed = httpx.ConnectError("down", request=request)
    pages = _Pages(
        {"data": [{"id": "claude-0"}], "has_more": True, "last_id": "claude-0"}, failed
    )
    listing = list_byok_models("anthropic", "sk-ant-test", get=pages)
    assert listing.outcome == "inconclusive"
    assert listing.models == ()


def test_more_promised_with_no_cursor_is_not_the_whole_list() -> None:
    """D6: has_more true with no last_id cannot be continued."""
    pages = _Pages({"data": [{"id": "claude-0"}], "has_more": True})
    listing = list_byok_models("anthropic", "sk-ant-test", get=pages)
    assert listing.outcome == "inconclusive"
    assert listing.models == ()
    assert len(pages.urls) == 1
