# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Hopscotch is offered as a key provider (OMN-26688, operator 2026-11-26: "add
https://docs.hopscotchlabs.ai/get-started/quickstart as new provider").

Facts, each read 2026-11-26 from Hopscotch's own docs (docs.hopscotchlabs.ai):
  - OpenAI-compatible; an OpenAI SDK's base URL is https://api.hopscotchlabs.ai/v1
    and chat goes to /v1/chat/completions (get-started/quickstart).
  - The key goes in `Authorization: Bearer <key>`; a key from the dashboard
    starts ub_live_ (get-started/authentication).
  - Keys are created on the dashboard's Keys screen at
    https://platform.hopscotchlabs.ai, never through the API (quickstart,
    authentication).
  - GET /v1/models answers {"object": "list", "data": [{"id": ...}]}; each id
    is what is sent as `model`, provider-qualified, as openai/gpt-4o-mini; a
    bare id is refused (api-reference/models/list-models, features/models).
  - "This site publishes no list of models" (features/models); every page's
    first request names openai/gpt-4o-mini, the one default.
  - Each listed model carries a `uniblock` block (api-reference/models/
    list-models, ModelExtension): display_name; context_window_tokens and
    max_output_tokens, integers or null; input_rate_thousandths and
    output_rate_thousandths, "thousandths of the currency unit per million
    tokens", in currency_code ("ISO 4217, or null where nobody recorded one"),
    omitted for a model priced per unit (image, audio, video); capabilities
    audio, json_mode, streaming, tools, vision, each 'yes', 'no' or 'unknown',
    and "`unknown` is not `no`" (features/model-capabilities).

Failure modes:
  H1  `onex models add hopscotch` is refused as an unknown provider.
  H2  the row posts or lists at a URL the docs do not give, names a default
      model the docs do not publish, or treats a platform key as held.
  H3  the docs' own /v1/models answer lists no model, or a different id.
  H4  /config does not offer Hopscotch, its key page is not the docs' one, or
      Local stops being the last choice.
  H5  a ub_live_ key pasted for another provider is not recognised as
      Hopscotch's.
  H6  the model table shows "-" for what Hopscotch's own answer states: its
      name, context, output limit, prices and capabilities (operator
      2026-11-26, on a screenshot of 774 models all "-": "check?", then "live
      fetch when fetch model all the details?").
  H7  an edge of that answer is misread (operator 2026-11-26: "cover all edge
      cases also"): a per-unit model's missing rate shown as free; a rate in
      a currency other than USD, or none recorded, shown as dollars; a 'no'
      or 'unknown' capability shown as supported; a null, negative, fractional,
      boolean or text number taken as a value; a missing or malformed block
      losing the model itself.
  H8  a refused key (401), no credit (402), a rate limit (429) or an empty
      list is not told apart in what the key window reads.
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from omnimarket.handlers.routing.byok_provider_backends import (
    customer_provider_catalogue,
    key_provider_hint,
    resolve_byok_provider_backend,
)

pytestmark = pytest.mark.unit

#: The list-models answer as api-reference/models/list-models shows it.
_LISTED = {
    "object": "list",
    "data": [
        {
            "id": "openai/gpt-4o-mini",
            "object": "model",
            "created": 1756080000,
            "owned_by": "openai",
            "uniblock": {
                "model_id": "openai/gpt-4o-mini",
                "display_name": "GPT-4o mini",
                "author": "openai",
                "context_window_tokens": 128000,
                "max_output_tokens": 16384,
                "currency_code": "USD",
                "endpoint_families": ["chat_completions"],
            },
        }
    ],
}


def test_hopscotch_is_in_the_customer_catalogue() -> None:
    """H1."""
    assert "hopscotch" in customer_provider_catalogue()


def test_onex_models_add_help_leaves_hopscotch_out() -> None:
    """H1: the installed `onex`. Its help reads the same with the flag on or off, so it
    does not name Hopscotch, which a feature flag keeps off by default (OMN-26688)."""
    onex = Path(sys.executable).parent / "onex"
    shown = subprocess.run(
        [str(onex), "models", "add", "--help"],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert shown.returncode == 0, shown.stderr
    said = " ".join(shown.stdout.split())
    providers = re.search(r"PROVIDER is ([a-z_, ]+?) or the id", said)
    assert providers is not None, shown.stdout
    named = re.split(r",\s*|\s+or\s+", providers.group(1))
    assert "openai" in named  # the control: the list was read
    assert "hopscotch" not in named


def test_the_row_is_what_the_docs_say() -> None:
    """H2."""
    row = resolve_byok_provider_backend("hopscotch")
    assert row is not None
    assert row.endpoint_url == "https://api.hopscotchlabs.ai/v1/chat/completions"
    assert row.models_url == "https://api.hopscotchlabs.ai/v1/models"
    assert row.models_auth != "x_api_key"  # a Bearer key
    assert row.key_page == "https://platform.hopscotchlabs.ai/"
    assert tuple(row.key_prefixes) == ("ub_live_",)
    assert tuple(row.default_models) == ("openai/gpt-4o-mini",)
    assert row.customer_chooses_model is True
    assert row.mirrors_house_rung is False  # the platform holds no Hopscotch key


def test_the_docs_model_list_answer_is_read() -> None:
    """H3."""
    from omnimarket.handlers.routing.byok_model_listing import list_byok_models

    listing = list_byok_models("hopscotch", "unused-fixture", get=lambda **_: _LISTED)
    assert listing.outcome == "listed"
    assert [m.id for m in listing.models] == ["openai/gpt-4o-mini"]


def _one(entry: dict) -> object:
    from omnimarket.handlers.routing.byok_model_listing import list_byok_models

    listing = list_byok_models(
        "hopscotch",
        "unused-fixture",
        get=lambda **_: {"object": "list", "data": [entry]},
    )
    assert listing.outcome == "listed"
    assert len(listing.models) == 1
    return listing.models[0]


def _entry(**uniblock: object) -> dict:
    block = {
        "model_id": "openai/gpt-4o-mini",
        "display_name": "GPT-4o mini",
        "author": "openai",
        "context_window_tokens": 128000,
        "max_output_tokens": 16384,
        "input_rate_thousandths": 150,
        "output_rate_thousandths": 600,
        "currency_code": "USD",
        "endpoint_families": ["chat_completions"],
        "provider_display_names": ["OpenAI"],
        "capabilities": {
            "audio": "no",
            "json_mode": "yes",
            "streaming": "yes",
            "tools": "yes",
            "vision": "unknown",
        },
    }
    block.update(uniblock)
    block = {k: v for k, v in block.items() if v is not _GONE}
    return {"id": "openai/gpt-4o-mini", "object": "model", "uniblock": block}


_GONE = object()


def test_the_model_table_gets_what_hopscotch_states() -> None:
    """H6: the docs' own example, read whole."""
    from omnimarket.handlers.model_catalog import EMPTY_INDEX, listed_catalog_entry

    model = _one(_entry())
    assert model.name == "GPT-4o mini"
    assert model.context_tokens == 128000
    assert model.max_output_tokens == 16384
    assert model.input_usd_per_million == 0.15
    assert model.output_usd_per_million == 0.6
    entry = listed_catalog_entry(
        "hopscotch", model, EMPTY_INDEX, provider_label="Hopscotch"
    )
    shown = {k for k, v in entry.capabilities.items() if v == "yes"}
    assert shown == {"Tools"}, entry.capabilities  # vision is 'unknown', not yes


@pytest.mark.parametrize(
    ("change", "expected_in", "expected_out"),
    [
        (
            {"input_rate_thousandths": _GONE, "output_rate_thousandths": _GONE},
            None,
            None,
        ),
        ({"output_rate_thousandths": _GONE}, 0.15, None),
        ({"currency_code": None}, None, None),
        ({"currency_code": "EUR"}, None, None),
        ({"currency_code": _GONE}, None, None),
        ({"input_rate_thousandths": 0, "output_rate_thousandths": 0}, 0.0, 0.0),
        ({"input_rate_thousandths": -1}, None, 0.6),
        ({"input_rate_thousandths": "150"}, None, 0.6),
        ({"input_rate_thousandths": True}, None, 0.6),
        ({"input_rate_thousandths": 1.5}, None, 0.6),
        ({"input_rate_thousandths": None}, None, 0.6),
        ({"input_rate_thousandths": 1}, 0.001, 0.6),
        ({"currency_code": "usd"}, 0.15, 0.6),
    ],
)
def test_a_price_is_shown_only_as_the_docs_define_it(
    change: dict, expected_in: float | None, expected_out: float | None
) -> None:
    """H7: dollars only for USD, never a missing rate as free."""
    model = _one(_entry(**change))
    assert model.input_usd_per_million == expected_in
    assert model.output_usd_per_million == expected_out


@pytest.mark.parametrize(
    "change",
    [
        {"context_window_tokens": None, "max_output_tokens": None},
        {"context_window_tokens": 0, "max_output_tokens": -5},
        {"context_window_tokens": "128000", "max_output_tokens": True},
        {"context_window_tokens": _GONE, "max_output_tokens": _GONE},
    ],
)
def test_an_unstated_limit_stays_unknown(change: dict) -> None:
    """H7."""
    model = _one(_entry(**change))
    assert model.context_tokens is None
    assert model.max_output_tokens is None


@pytest.mark.parametrize(
    ("words", "expected"),
    [
        ({"tools": "no", "json_mode": "no", "vision": "no", "audio": "no"}, set()),
        ({"tools": "unknown", "json_mode": "unknown", "vision": "unknown"}, set()),
        ({"tools": "yes", "vision": "yes"}, {"Tools", "Vision"}),
        ({"tools": True, "vision": 1}, set()),  # only the word yes counts
        ({"tools": "YES"}, set()),
    ],
)
def test_only_a_yes_is_a_capability(words: dict, expected: set) -> None:
    """H7: 'no' and 'unknown' are non-empty strings; neither is a yes."""
    from omnimarket.handlers.model_catalog import EMPTY_INDEX, listed_catalog_entry

    capabilities = {
        "audio": "unknown",
        "json_mode": "unknown",
        "streaming": "unknown",
        "tools": "unknown",
        "vision": "unknown",
    }
    capabilities.update(words)
    model = _one(_entry(capabilities=capabilities))
    entry = listed_catalog_entry(
        "hopscotch", model, EMPTY_INDEX, provider_label="Hopscotch"
    )
    assert {k for k, v in entry.capabilities.items() if v == "yes"} == expected


@pytest.mark.parametrize(
    "uniblock", [_GONE, None, "text", [], {"capabilities": "yes"}, {"display_name": 7}]
)
def test_a_missing_or_malformed_block_keeps_the_model(uniblock: object) -> None:
    """H7: the model is still listed by its id; nothing is invented."""
    entry: dict = {"id": "openai/gpt-4o-mini", "object": "model"}
    if uniblock is not _GONE:
        entry["uniblock"] = uniblock
    model = _one(entry)
    assert model.id == "openai/gpt-4o-mini"
    assert model.name == "openai/gpt-4o-mini"
    assert model.context_tokens is None
    assert model.input_usd_per_million is None
    assert model.supported_parameters == ()
    assert model.input_modalities == ()


def _refused(status: int, body: dict):
    import httpx

    def get(**_: object) -> None:
        request = httpx.Request("GET", "https://api.hopscotchlabs.ai/v1/models")
        response = httpx.Response(status, json=body, request=request)
        raise httpx.HTTPStatusError("refused", request=request, response=response)

    return get


@pytest.mark.parametrize(
    ("status", "code", "outcome"),
    [
        (401, "invalid_api_key", "rejected"),
        (401, "key_expired", "rejected"),
        (402, "insufficient_credit", "billing"),
        (429, "rate_limit_exceeded", "inconclusive"),
        (500, "server_error", "inconclusive"),
    ],
)
def test_each_refusal_is_told_apart(status: int, code: str, outcome: str) -> None:
    """H8: the docs' own error envelope; the key never comes back."""
    from omnimarket.handlers.routing.byok_model_listing import list_byok_models

    key = "ub_live_" + "k" * 43
    body = {"error": {"message": f"{code} for {key}", "type": code, "code": code}}
    listing = list_byok_models("hopscotch", key, get=_refused(status, body))
    assert listing.outcome == outcome
    assert listing.models == ()
    assert key not in (listing.message or "")


def test_an_empty_list_says_so() -> None:
    """H8."""
    from omnimarket.handlers.routing.byok_model_listing import list_byok_models

    listing = list_byok_models(
        "hopscotch", "unused-fixture", get=lambda **_: {"object": "list", "data": []}
    )
    assert listing.outcome == "listed"
    assert listing.models == ()
    assert listing.message


def test_config_offers_hopscotch_before_local() -> None:
    """H4."""
    from omnimarket.nodes.node_onex_setup_effect import cli_tui

    offered = cli_tui._picker()
    names = [e.provider for e in offered]
    assert "hopscotch" in names
    assert names[-1] == "local"
    entry = next(e for e in offered if e.provider == "hopscotch")
    assert entry.label == "Hopscotch"
    assert "platform.hopscotchlabs.ai" in entry.hint
    assert cli_tui._key_host("hopscotch") == "api.hopscotchlabs.ai"


def test_a_hopscotch_key_is_recognised_under_another_provider() -> None:
    """H5."""
    key = "ub_live_" + "a" * 43
    assert key_provider_hint(key, for_provider="openai") == "hopscotch"


@pytest.fixture(autouse=True)
def _hopscotch_turned_on(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """These tests are Hopscotch itself: its feature flag is on for them (OMN-26688)."""
    from omnimarket.handlers.routing import (
        byok_catalog_providers,
        byok_provider_backends,
    )
    from omnimarket.nodes.node_onex_setup_effect.models import (
        model_setup_provider_traits,
    )

    caches = (
        byok_catalog_providers.load_byok_catalog_added_providers,
        byok_catalog_providers.load_byok_catalog_providers,
        byok_provider_backends.load_byok_provider_catalog,
        byok_provider_backends.load_byok_plan_catalog,
        model_setup_provider_traits._listed_picker,
    )
    monkeypatch.setenv("ONEX_ENABLE_HOPSCOTCH", "1")
    for cache in caches:
        cache.cache_clear()
    yield
    monkeypatch.delenv("ONEX_ENABLE_HOPSCOTCH")
    for cache in caches:
        cache.cache_clear()
