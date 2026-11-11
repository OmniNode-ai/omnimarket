# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Every model a customer's key can use, with the details a picker shows (OMN-23413).

Failure modes:
  F1. Details a provider publishes (name, context window, price per million
      tokens) are dropped, so the picker shows bare ids.
  F2. A per-token price string is shown as-is instead of per million tokens.
  F3. Google's ``models/`` prefix reaches the picker, so the id saved is wrong.
  F4. A refused key or a failed read crashes, or reads as an empty list.
  F5. The key appears in the result or in any header but the auth header.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from omnimarket.handlers.routing.byok_model_listing import list_byok_models

pytestmark = pytest.mark.unit

_KEY = "sk-test-not-a-real-key"


def _get(body: Any, seen: list[dict[str, Any]] | None = None) -> Any:
    def get(*, url: str, timeout_seconds: float, extra_headers: dict[str, str]) -> Any:
        if seen is not None:
            seen.append({"url": url, "headers": extra_headers})
        return body

    return get


def test_openrouter_rows_carry_name_context_and_price_per_million() -> None:
    """F1, F2."""
    seen: list[dict[str, Any]] = []
    body = {
        "data": [
            {
                "id": "qwen/qwen3.8-27b",
                "name": "Qwen: Qwen3.8 27B",
                "context_length": 131072,
                "pricing": {"prompt": "0.00000015", "completion": "0.0000006"},
            },
            {"id": "openrouter/free-model", "name": "Free", "pricing": {"prompt": "0"}},
        ]
    }
    listing = list_byok_models("openrouter", _KEY, get=_get(body, seen))
    assert listing.outcome == "listed"
    by_id = {m.id: m for m in listing.models}
    first = by_id["qwen/qwen3.8-27b"]
    assert (first.id, first.name, first.context_tokens) == (
        "qwen/qwen3.8-27b",
        "Qwen: Qwen3.8 27B",
        131072,
    )
    assert first.input_usd_per_million == pytest.approx(0.15)
    assert first.output_usd_per_million == pytest.approx(0.6)
    assert by_id["openrouter/free-model"].input_usd_per_million == 0.0
    assert seen[0]["url"].endswith("/models")
    assert seen[0]["headers"]["Authorization"] == f"Bearer {_KEY}"


def test_google_names_lose_the_models_prefix_and_keep_their_limits() -> None:
    """F3."""
    body = {
        "models": [
            {
                "name": "models/gemini-2.5-flash",
                "displayName": "Gemini 2.5 Flash",
                "inputTokenLimit": 1048576,
            }
        ]
    }
    listing = list_byok_models("gemini", _KEY, get=_get(body))
    assert listing.models[0].id == "gemini-2.5-flash"
    assert listing.models[0].name == "Gemini 2.5 Flash"
    assert listing.models[0].context_tokens == 1048576


def test_bare_openai_ids_are_listed_sorted_with_no_invented_details() -> None:
    body = {"data": [{"id": "gpt-5-mini", "owned_by": "openai"}, {"id": "gpt-5"}]}
    listing = list_byok_models("openai", _KEY, get=_get(body))
    assert [m.id for m in listing.models] == ["gpt-5", "gpt-5-mini"]
    assert listing.models[0].context_tokens is None
    assert listing.models[0].input_usd_per_million is None


def test_a_refused_key_is_a_typed_outcome_with_the_provider_message() -> None:
    """F4, F5."""

    def refused(**_: Any) -> Any:
        request = httpx.Request("GET", "https://example.invalid/models")
        raise httpx.HTTPStatusError(
            "401",
            request=request,
            response=httpx.Response(
                401, request=request, text='{"error":{"message":"Invalid API key"}}'
            ),
        )

    listing = list_byok_models("openai", _KEY, get=refused)
    assert listing.outcome == "rejected"
    assert listing.models == ()
    assert "Invalid API key" in (listing.message or "")
    assert _KEY not in listing.model_dump_json()


def test_a_failed_read_is_inconclusive_not_empty() -> None:
    """F4."""

    def broken(**_: Any) -> Any:
        raise httpx.ConnectError("down")

    listing = list_byok_models("openrouter", _KEY, get=broken)
    assert listing.outcome == "inconclusive"
    assert listing.message


def test_a_provider_with_no_catalogue_entry_is_refused() -> None:
    listing = list_byok_models("ollama", _KEY, get=_get({}))
    assert listing.outcome == "unsupported"


@pytest.mark.parametrize(
    ("body", "expected_ids"),
    [
        ([], []),
        ({"data": "not a list"}, []),
        ({"data": [None, 3, {"id": ""}, {"id": "  "}, {"owned_by": "x"}]}, []),
        ({"data": [{"id": "b"}, {"id": "a"}, {"id": "b"}]}, ["a", "b"]),
    ],
)
def test_odd_bodies_never_crash_and_list_only_real_ids(
    body: Any, expected_ids: list[str]
) -> None:
    listing = list_byok_models("openrouter", _KEY, get=_get(body))
    assert listing.outcome == "listed"
    assert [m.id for m in listing.models] == expected_ids
    if not expected_ids:
        assert listing.message == "the provider listed no models for this key"


def test_unusable_prices_and_context_are_none_not_zero() -> None:
    body = {
        "data": [
            {
                "id": "router/auto",
                "context_length": "not a number",
                "pricing": {"prompt": "-1", "completion": "n/a"},
                "top_provider": {"context_length": 0},
            },
            {"id": "x/y", "context_length": "8192"},
        ]
    }
    auto, other = list_byok_models("openrouter", _KEY, get=_get(body)).models
    assert auto.input_usd_per_million is None
    assert auto.output_usd_per_million is None
    assert auto.context_tokens is None
    assert other.context_tokens == 8192


def test_an_empty_key_is_refused_before_any_request() -> None:
    seen: list[dict[str, Any]] = []
    listing = list_byok_models("openai", "   ", get=_get({"data": []}, seen))
    assert listing.outcome == "rejected"
    assert seen == []


@pytest.mark.parametrize(
    ("status", "outcome"), [(402, "billing"), (503, "inconclusive")]
)
def test_billing_and_server_errors_are_typed(status: int, outcome: str) -> None:
    def failing(**_: Any) -> Any:
        request = httpx.Request("GET", "https://example.invalid/models")
        raise httpx.HTTPStatusError(
            str(status),
            request=request,
            response=httpx.Response(status, request=request, text=f"echo {_KEY}"),
        )

    listing = list_byok_models("openrouter", _KEY, get=failing)
    assert listing.outcome == outcome
    assert _KEY not in listing.model_dump_json()


def test_a_timeout_is_inconclusive_and_names_no_request_detail() -> None:
    def slow(**_: Any) -> Any:
        raise httpx.ReadTimeout(f"timed out with {_KEY}")

    listing = list_byok_models("gemini", _KEY, get=slow)
    assert listing.outcome == "inconclusive"
    assert _KEY not in listing.model_dump_json()
