# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26688: OpenCode's own model list, read live, as its docs and its answer give it.

Operator 2026-11-26: "check official docs and implement it live model fetching
and everything from their docs". opencode.ai/docs (read 2026-11-26): "Fetch the
full list of available models and their metadata from the models endpoint";
prices are USD per 1M tokens; "Free-tier chat models are only available from
within OpenCode. Other harnesses receive HTTP 403 for those models". "Every API
serves every paid and Go text model", so a paid chat model whose recommended
API is not chat completions is still reached there.

The fixture is six entries of OpenCode's real answer at
https://opencode.ai/inference/openai/v1/models, recorded 2026-11-26.

Failure modes, each with a test:
  P1 a model onex cannot use is offered: a free-tier one (403 outside OpenCode),
     or one that does not chat (embedding, image, realtime).
  P2 a detail OpenCode publishes is lost or misread: name, context, output
     limit, USD price per 1M, tools, reasoning, structured output, inputs.
  P3 a price OpenCode does not state reads as zero (free) instead of unknown.
  P4 the OpenCode reading leaks into another provider whose rows also carry a
     ``type`` (Anthropic's say "model").
  P5 the read goes anywhere but the catalogue's models_url, or without the key
     as a Bearer header.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from omnimarket.handlers.routing import byok_model_listing
from omnimarket.handlers.routing.byok_model_listing import list_byok_models

_RECORDED = json.loads(
    (Path(__file__).parent / "fixtures" / "opencode_models_2026-11-26.json").read_text(
        encoding="utf-8"
    )
)


def _listing(body: dict[str, Any] | None = None) -> dict[str, Any]:
    listing = list_byok_models(
        "opencode", "unused-fixture", get=lambda **_: body or _RECORDED
    )
    assert listing.outcome == "listed", listing
    return {model.id: model for model in listing.models}


def test_only_paid_chat_models_are_offered() -> None:
    """P1."""
    recorded = {entry["id"] for entry in _RECORDED["data"]}
    assert {"big-pickle", "text-embedding-3-small", "gpt-live-1"} <= recorded
    assert sorted(_listing()) == ["claude-sonnet-5-5", "deepseek-v4-pro"]


def test_each_detail_is_read_as_opencode_states_it() -> None:
    """P2."""
    models = _listing()
    deepseek = models["deepseek-v4-pro"]
    assert deepseek.name == "DeepSeek V4 Pro"
    assert deepseek.context_tokens == 1_000_000
    assert deepseek.max_output_tokens == 384_000
    assert deepseek.input_usd_per_million == 1.74
    assert deepseek.output_usd_per_million == 3.48
    assert set(deepseek.supported_parameters) == {
        "tools",
        "reasoning",
        "structured_outputs",
    }
    assert deepseek.input_modalities == ("text",)
    claude = models["claude-sonnet-5-5"]
    assert claude.name == "Claude Sonnet 5.5"
    assert claude.input_usd_per_million == 2.0
    assert claude.output_usd_per_million == 10.0
    assert set(claude.supported_parameters) == {"tools", "reasoning"}
    assert claude.input_modalities == ("text", "image", "file")


def test_an_unstated_price_is_unknown_not_free() -> None:
    """P3."""
    body = copy.deepcopy(_RECORDED)
    entry = next(e for e in body["data"] if e["id"] == "deepseek-v4-pro")
    del entry["cost"]
    model = _listing(body)["deepseek-v4-pro"]  # still offered: not stated free
    assert model.input_usd_per_million is None
    assert model.output_usd_per_million is None


def test_another_providers_typed_rows_are_untouched() -> None:
    """P4."""
    entry = {"type": "model", "id": "claude-example", "display_name": "Example"}
    listed = byok_model_listing._listed(entry)
    assert listed is not None
    assert listed.name == "Example"


def test_the_read_goes_to_the_catalogue_url_with_the_key() -> None:
    """P5."""
    seen: list[dict[str, Any]] = []

    def get(**kwargs: Any) -> dict[str, Any]:
        seen.append(kwargs)
        return _RECORDED

    list_byok_models("opencode", "fixture-key", get=get)
    assert [call["url"] for call in seen] == [
        "https://opencode.ai/inference/openai/v1/models"
    ]
    assert seen[0]["extra_headers"].get("Authorization") == "Bearer fixture-key"
