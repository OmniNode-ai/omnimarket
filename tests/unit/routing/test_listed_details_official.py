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
