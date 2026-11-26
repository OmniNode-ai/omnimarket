# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Every OpenAI-compatible provider in the models.dev catalogue, each under its own key (OMN-26688).

The operator asked on 2026-11-26 for every API-key provider and chose one key per
provider. A shipped, reviewed list (``byok_catalog_providers.yaml``, generated from
models.dev) names each provider whose SDK package is ``@ai-sdk/openai-compatible``
and whose base URL is a fixed ``https://`` URL. Each one is set up, routed, named
and priced under its own id. Coding and token plans are offered only with a ruling
row (``byok_catalog_plan_rulings.yaml``), and this ticket ships none.

Failure modes, each a test below:
  C1  ``onex models add deepseek`` is refused as an unknown provider.
  C2  a second catalogue provider's key replaces the first (one shared slot).
  C3  the key is sent to a URL other than the list's (models list or chat).
  C4  an id not on the list resolves to a route (fail-open).
  C5  a coding or token plan resolves to a route, or is offered, without a ruling.
  C6  the list takes a templated, local or plain-http URL, a plan, a provider with
      its own SDK, or an id that duplicates a declared provider's endpoint.
  C7  metering or the TUI names the provider by its id or as OpenAI-compatible, or
      a call on it is left unpriced when models.dev prices the model.
  C8  the Config picker does not list the catalogue providers, or a declared
      provider stops being listed first.
  C9  the shipped list drifts from what the generator writes from the same input.
  C10 Z.AI is shown as GLM, or ``zai`` is refused, or the old name ``glm`` stops
      working for a key already stored under it (operator 2026-11-26).
  C11 a docs link that is one model's page, not the provider's, is kept.
  C12 verify pass 2026-11-26, 80 columns: ``onex models add --help`` broke
      "anthropic" across two lines, so the usage named no such provider.
  C13 ``onex models zai`` gave no hint, while ``onex models deepseek`` said
      "Did you mean 'onex models add deepseek'?".
  C14 a provider the operator names that models.dev does not list (Hopscotch,
      2026-11-26) cannot be added, routed or picked, or its key goes elsewhere.
  C15 a row added by hand skips the list's rules (a templated or local URL, a
      plan, an id taken by a declared or listed provider) or carries no source.

Fakes only: the model list and the pinned delegation are stubs; no network.
  C16 operator 2026-11-26 ("are they all working?"): a listed provider whose
      address is gone (shut down, no DNS record, refused, the web site and
      not the API) is offered anyway, and fails only after the user pastes a
      key. Checked twice on 2026-11-26, read only and without a key.
  C17 a withheld row names no reason or check, or outlives its provider on
      the models.dev list, so nobody can tell why it is held or when to drop it.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

from omnimarket.cli.cli_secret import models_group
from omnimarket.handlers import model_catalog
from omnimarket.handlers.routing import byok_model_discovery
from omnimarket.handlers.routing.byok_provider_backends import (
    byok_backend_provider,
    byok_model_price_with_source,
    customer_provider_catalogue,
    resolve_byok_provider_backend,
)
from omnimarket.inference.local_byok_credential_adapter import (
    LocalByokCredentialStore,
    registered_local_byok_providers,
)
from omnimarket.nodes.node_model_setup_effect.handlers import handler_model_setup

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[3]
_LIST = _ROOT / "src/omnimarket/handlers/routing/byok_catalog_providers.yaml"
_RULINGS = _ROOT / "src/omnimarket/handlers/routing/byok_catalog_plan_rulings.yaml"
_ADDED = _ROOT / "src/omnimarket/handlers/routing/byok_catalog_added_providers.yaml"
_WITHHELD = (
    _ROOT / "src/omnimarket/handlers/routing/byok_catalog_withheld_providers.yaml"
)
#: Checked 2026-11-26, twice: shut down (HTTP 410), origin down (521), no DNS
#: record, connection refused, the web site not the API (406), no route (404),
#: and a sign-in token where a key would go.
_DEAD = (
    "llmtech",
    "aigrow",
    "clarifai",
    "drun",
    "inference",
    "jad-ai",
    "github-copilot",
)

#: Two providers from the shipped list, read from models.dev on 2026-11-26: one base
#: URL with no path and one with a trailing slash, one id with a hyphen.
_DEEPSEEK = ("deepseek", "DeepSeek", "https://api.deepseek.com")
_FIREWORKS = ("fireworks-ai", "Fireworks AI", "https://api.fireworks.ai/inference/v1")
_KEYS = {
    "deepseek": "sk-synthetic-omn26688-deepseek-never-printed",
    "fireworks-ai": "fw-synthetic-omn26688-fireworks-never-printed",
}


@pytest.fixture(autouse=True)
def local_store_at_tmp(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    db_path = tmp_path / "delegation" / "delegation.sqlite"
    db_path.parent.mkdir()
    for module in (
        "omnimarket.inference.local_byok_credential_adapter",
        "omnimarket.nodes.node_model_setup_effect.handlers.handler_model_setup",
    ):
        monkeypatch.setattr(f"{module}.default_evidence_db_path", lambda: db_path)
    return db_path


class _Models:
    """A provider's list-models endpoint: records each URL and key it was sent."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(
        self,
        *,
        url: str,
        api_key: str | None = None,
        extra_headers: dict[str, str] | None = None,
        **_: Any,
    ) -> Any:
        sent = api_key or (extra_headers or {}).get("Authorization", "")
        self.calls.append((url, str(sent)))
        return {"data": [{"id": "fixture-model-1", "object": "model"}]}


class _Delegate:
    """Stands in for ``onex delegate --backend-id``; records each pin it got."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.pins: list[str] = []

    def __call__(self, backend_id: str) -> tuple[int, str, str]:
        import json

        self.pins.append(backend_id)
        receipt = self.tmp_path / f"run-{len(self.pins)}" / "receipt.json"
        receipt.parent.mkdir()
        receipt.write_text(
            json.dumps(
                {
                    "status": "success",
                    "backend_id": backend_id,
                    "model": "fixture-model-1",
                }
            ),
            encoding="utf-8",
        )
        return 0, '{"status": "success"}\n', f"delegate artifacts: {receipt}"


def _arrange(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[_Models, _Delegate]:
    models, delegate = _Models(), _Delegate(tmp_path)
    monkeypatch.setattr(byok_model_discovery, "get_models_json", models)
    monkeypatch.setattr(handler_model_setup, "run_pinned_delegation", delegate)
    return models, delegate


def _add(provider: str) -> Any:
    return CliRunner().invoke(
        models_group,
        ["add", provider, "--model", "fixture-model-1"],
        input=f"{_KEYS[provider]}\n",
    )


# --------------------------------------------------------------------------- C1-C3


def test_onex_models_add_takes_a_catalogue_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """C1."""
    _arrange(monkeypatch, tmp_path)
    result = _add("deepseek")
    assert result.exit_code == 0, result.output
    assert "deepseek" in customer_provider_catalogue()


def test_two_catalogue_providers_keep_their_own_keys(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """C2: the second key is stored beside the first, never in its place."""
    _arrange(monkeypatch, tmp_path)
    for provider in ("deepseek", "fireworks-ai"):
        result = _add(provider)
        assert result.exit_code == 0, result.output
    store = LocalByokCredentialStore()
    for provider, key in _KEYS.items():
        assert asyncio.run(store.get_secret(f"llm.{provider}.api_key")) == key
    assert {"deepseek", "fireworks-ai"} <= set(registered_local_byok_providers())


def test_each_key_goes_only_to_its_own_listed_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """C3: the model list is read at the list's URL with that provider's own key,
    and the test delegation is pinned to that provider's own backend."""
    models, delegate = _arrange(monkeypatch, tmp_path)
    for provider in ("deepseek", "fireworks-ai"):
        assert _add(provider).exit_code == 0
    by_url = dict(models.calls)
    assert set(by_url) == {f"{_DEEPSEEK[2]}/models", f"{_FIREWORKS[2]}/models"}
    assert _KEYS["deepseek"] in by_url[f"{_DEEPSEEK[2]}/models"]
    assert _KEYS["fireworks-ai"] in by_url[f"{_FIREWORKS[2]}/models"]
    assert delegate.pins == ["byok-deepseek", "byok-fireworks-ai"]


@pytest.mark.parametrize("provider", [_DEEPSEEK, _FIREWORKS], ids=lambda p: p[0])
def test_a_catalogue_provider_routes_to_its_listed_url(
    provider: tuple[str, str, str],
) -> None:
    """C3: the route posts to the list's base URL, as a customer-only surface."""
    pid, _, base = provider
    row = resolve_byok_provider_backend(pid)
    assert row is not None
    assert row.endpoint_url == f"{base}/chat/completions"
    assert row.models_url == f"{base}/models"
    assert row.backend_id == f"byok-{pid}"
    assert row.mirrors_house_rung is False  # the platform holds no key for it
    assert row.endpoint_from_registration is False  # the URL is never typed in
    assert row.customer_chooses_model is True


# --------------------------------------------------------------------------- C4-C5


def test_an_id_not_on_the_list_still_has_no_route() -> None:
    """C4: positive control first, so a None below is not a broken lookup."""
    assert resolve_byok_provider_backend("deepseek") is not None
    assert resolve_byok_provider_backend("not-a-catalogue-provider") is None
    assert resolve_byok_provider_backend("deepseek", "coding_plan") is None


@pytest.mark.parametrize(
    "plan_id", ["zai-coding-plan", "kimi-code-plan-global", "alibaba-token-plan"]
)
def test_a_coding_or_token_plan_has_no_route_and_is_not_offered(plan_id: str) -> None:
    """C5: this ticket ships with the rulings file empty, so no plan is offered."""
    from omnimarket.handlers.routing.byok_catalog_providers import (
        load_byok_catalog_providers,
    )

    assert resolve_byok_provider_backend(plan_id) is None
    assert plan_id not in load_byok_catalog_providers()
    assert plan_id not in customer_provider_catalogue()


def test_the_rulings_file_ships_empty() -> None:
    """C5: each plan needs its own doc ruling and the operator's approval."""
    document = yaml.safe_load(_RULINGS.read_text())
    assert document["rulings"] == []


# --------------------------------------------------------------------------- C6, C9

_DOC = {
    # kept: fixed https URL, OpenAI-compatible package
    "keepme": {
        "id": "keepme",
        "name": "Keep Me",
        "npm": "@ai-sdk/openai-compatible",
        "api": "https://api.keepme.example/v1/",
        "doc": "https://keepme.example/docs",
        "models": {"m": {}},
    },
    # each of these is dropped, for the reason in its id
    "own-sdk": {
        "name": "Own SDK",
        "npm": "@ai-sdk/groq",
        "api": "https://api.own.example/v1",
        "doc": "https://own.example",
        "models": {"m": {}},
    },
    "no-url": {
        "name": "No URL",
        "npm": "@ai-sdk/openai-compatible",
        "doc": "https://nourl.example",
        "models": {"m": {}},
    },
    "templated": {
        "name": "Templated",
        "npm": "@ai-sdk/openai-compatible",
        "api": "https://${ACCOUNT}.templated.example/v1",
        "doc": "https://templated.example",
        "models": {"m": {}},
    },
    "plain-http": {
        "name": "Plain HTTP",
        "npm": "@ai-sdk/openai-compatible",
        "api": "http://127.0.0.1:1234/v1",
        "doc": "https://local.example",
        "models": {"m": {}},
    },
    "acme-coding-plan": {
        "name": "Acme Coding Plan",
        "npm": "@ai-sdk/openai-compatible",
        "api": "https://api.acme.example/coding/v1",
        "doc": "https://acme.example",
        "models": {"m": {}},
    },
    "acme-tokens": {
        "name": "Acme Token Plan",
        "npm": "@ai-sdk/openai-compatible",
        "api": "https://api.acme.example/tokens/v1",
        "doc": "https://acme.example",
        "models": {"m": {}},
    },
    "dup-of-declared": {
        "name": "Duplicate",
        "npm": "@ai-sdk/openai-compatible",
        "api": "https://api.declared.example/v1",
        "doc": "https://declared.example",
        "models": {"m": {}},
    },
    "bad.id": {
        "name": "Bad Id",
        "npm": "@ai-sdk/openai-compatible",
        "api": "https://api.badid.example/v1",
        "doc": "https://badid.example",
        "models": {"m": {}},
    },
}


def test_the_generator_keeps_only_fixed_https_openai_compatible_providers() -> None:
    """C6: one fixture per rule; only the provider that passes every rule is kept."""
    from omnimarket.handlers.routing.byok_catalog_providers import (
        select_catalog_providers,
    )

    kept = select_catalog_providers(
        _DOC,
        declared_ids=frozenset({"openai"}),
        declared_hosts=frozenset({"api.declared.example"}),
        rulings=(),
    )
    assert [(p.id, p.name, p.api, p.doc) for p in kept] == [
        (
            "keepme",
            "Keep Me",
            "https://api.keepme.example/v1",
            "https://keepme.example/docs",
        )
    ]


def test_a_plan_is_kept_only_with_a_ruling_row() -> None:
    """C5, C6: positive control: the same plan with a ruling row is kept."""
    from omnimarket.handlers.routing.byok_catalog_providers import (
        ModelByokCatalogPlanRuling,
        select_catalog_providers,
    )

    ruling = ModelByokCatalogPlanRuling(
        provider="acme-coding-plan",
        doc_url="https://acme.example/terms",
        quote="API keys may be used in third-party tools.",
        read_on="2026-11-26",
    )
    kept = select_catalog_providers(
        _DOC,
        declared_ids=frozenset(),
        declared_hosts=frozenset({"api.declared.example"}),
        rulings=(ruling,),
    )
    assert [p.id for p in kept] == ["acme-coding-plan", "keepme"]


def test_the_shipped_list_follows_every_rule() -> None:
    """C6: read the shipped file itself, not the generator."""
    document = yaml.safe_load(_LIST.read_text())
    rows = document["providers"]
    assert len(rows) > 200  # 254 when read 2026-11-26
    ids = [row["id"] for row in rows]
    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids)
    for row in rows:
        assert row["api"].startswith("https://"), row
        assert "$" not in row["api"], row
        assert "{" not in row["api"], row
        assert "plan" not in row["id"], row
        assert "coding" not in row["id"], row
    # an id whose endpoint a declared provider already serves (z.ai is "glm")
    assert "zai" not in ids
    assert "reflection" not in ids


def test_the_shipped_list_is_what_the_generator_writes() -> None:
    """C9: the list round-trips through the generator's own writer."""
    from omnimarket.handlers.routing.byok_catalog_providers import (
        load_generated_catalog_providers,
        render_catalog_providers,
    )

    shipped = _LIST.read_text()
    assert (
        render_catalog_providers(tuple(load_generated_catalog_providers().values()))
        == shipped
    )


# --------------------------------------------------------------------------- C7


def test_metering_names_and_prices_a_catalogue_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C7: one recorded call on DeepSeek is attributed to it and priced from models.dev."""
    from omnimarket.handlers.model_provider_labels import MODEL_LABELS

    index = model_catalog.ModelsDevIndex(
        models={
            ("deepseek", "deepseek-chat"): {"cost": {"input": 0.27, "output": 1.1}}
        },
        fetched_at="2026-11-26T06:08:00Z",
        stale=False,
        error=None,
    )
    monkeypatch.setattr(model_catalog, "pricing_models_dev_index", lambda: index)
    assert byok_backend_provider("byok-deepseek") == "deepseek"
    assert MODEL_LABELS["deepseek"] == "DeepSeek"
    priced = byok_model_price_with_source(
        "byok-deepseek",
        f"{_DEEPSEEK[2]}/chat/completions",
        "deepseek-chat",
        models_dev_price=model_catalog.models_dev_price_with_read_time,
    )
    assert priced == (
        (Decimal("0.27"), Decimal("1.1")),
        "models.dev",
        "2026-11-26T06:08:00Z",
    )


def test_the_models_dev_index_keeps_catalogue_providers() -> None:
    """C7: a refreshed models.dev copy keeps the prices of listed providers."""
    found = model_catalog._index(
        {"deepseek": {"models": {"deepseek-chat": {"cost": {"input": 1}}}}}
    )
    assert ("deepseek", "deepseek-chat") in found


# --------------------------------------------------------------------------- C8


def test_the_config_picker_lists_catalogue_providers_after_the_declared_ones() -> None:
    """C8: OMN-26620's list sorts them A-Z; this only needs them offered."""
    from omnimarket.nodes.node_onex_setup_effect.models.model_setup_provider_traits import (
        setup_provider_traits,
    )

    traits = setup_provider_traits()
    picker = traits.picker_providers()
    names = [p.provider for p in picker]
    declared = [p.provider for p in traits.picker]
    assert [n for n in names if n in declared] == declared  # the file's own order
    assert names[-1] == "local"  # a server the user runs stays last
    assert len(names) == len(declared) + len(set(names) - set(declared))
    by_id = {p.provider: p for p in picker}
    assert by_id["deepseek"].label == "DeepSeek"
    assert by_id["fireworks-ai"].label == "Fireworks AI"
    assert "zai-coding-plan" not in by_id


# --------------------------------------------------------------------------- C10, C11


def test_z_ai_is_named_as_its_own_docs_name_it() -> None:
    """C10: docs.z.ai (read 2026-11-26) calls the provider Z.AI ("Z.AI provides
    interfaces compatible with OpenAI API"); GLM names its model family and the
    separate GLM Coding Plan. Keys stay stored under the id ``glm``."""
    from omnimarket.handlers.model_provider_labels import (
        MODEL_LABELS,
        ModelProviderChoice,
    )

    assert MODEL_LABELS["glm"] == "Z.AI"
    choice = ModelProviderChoice()
    assert choice.convert("zai", None, None) == "glm"
    assert choice.convert("ZAI", None, None) == "glm"
    assert choice.convert("glm", None, None) == "glm"  # the old name still works


def test_a_docs_link_to_one_models_page_is_not_kept() -> None:
    """C11: the link a provider is shown with is its own page, not one model's."""
    from omnimarket.handlers.routing.byok_catalog_providers import (
        select_catalog_providers,
    )

    document = {
        "modelpage": {
            "name": "Model Page",
            "npm": "@ai-sdk/openai-compatible",
            "api": "https://api.modelpage.example/v1",
            "doc": "https://modelpage.example/models/some-model-7b",
            "models": {"some-model-7b": {}},
        }
    }
    kept = select_catalog_providers(
        document, declared_ids=frozenset(), declared_hosts=frozenset(), rulings=()
    )
    assert [(p.id, p.doc) for p in kept] == [("modelpage", None)]


# --------------------------------------------------------------------------- C12-C13

_SHOWN_NAMES = (
    "gemini",
    "openai",
    "openrouter",
    "reflection",
    "anthropic",
    "zai",
    "hopscotch",
    "local",
    "openai_compatible",
)


def test_the_add_help_names_every_provider_whole_at_80_columns() -> None:
    """C12."""
    from omnimarket.handlers.model_provider_labels import SHOWN_DECLARED

    assert SHOWN_DECLARED == _SHOWN_NAMES, "name the new provider in the add help"
    result = CliRunner().invoke(models_group, ["add", "--help"], terminal_width=80)
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    usage = lines[: next(i for i, line in enumerate(lines) if not line.strip())]
    glued = "".join(line.strip() for line in usage)
    spaced = " ".join(line.strip() for line in usage)
    words = set(spaced.replace("|", " ").replace("{", " ").replace("}", " ").split())
    for name in _SHOWN_NAMES:
        if name in glued:
            assert name in words, (name, usage)
    import re

    said = " ".join(result.output.split())
    for name in _SHOWN_NAMES:
        assert re.search(rf"\b{name}\b", said), (name, result.output)
    assert not re.search(r"\bglm\b", said), result.output  # Z.AI is zai


def test_a_bare_zai_is_pointed_at_models_add() -> None:
    """C13."""
    result = CliRunner().invoke(models_group, ["zai"])
    assert result.exit_code == 2, result.output
    assert "onex models add zai" in result.output, result.output


# --------------------------------------------------------------------------- C14-C15

#: docs.hopscotchlabs.ai/get-started/quickstart (read 2026-11-26): "An OpenAI SDK
#: base_url" is https://api.hopscotchlabs.ai/v1; keys start ub_live_.
_HOPSCOTCH = ("hopscotch", "Hopscotch", "https://api.hopscotchlabs.ai/v1")
_HOPSCOTCH_KEY = "ub_live_synthetic-omn26688-hopscotch-never-printed"


def test_hopscotch_is_added_routed_and_picked_under_its_own_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """C14."""
    pid, name, base = _HOPSCOTCH
    models, delegate = _arrange(monkeypatch, tmp_path)
    result = CliRunner().invoke(
        models_group,
        ["add", pid, "--model", "fixture-model-1"],
        input=f"{_HOPSCOTCH_KEY}\n",
    )
    assert result.exit_code == 0, result.output
    assert dict(models.calls) == {
        f"{base}/models": dict(models.calls)[f"{base}/models"]
    }
    assert _HOPSCOTCH_KEY in dict(models.calls)[f"{base}/models"]
    assert delegate.pins == [f"byok-{pid}"]
    store = LocalByokCredentialStore()
    assert asyncio.run(store.get_secret(f"llm.{pid}.api_key")) == _HOPSCOTCH_KEY
    row = resolve_byok_provider_backend(pid)
    assert row is not None
    assert row.endpoint_url == f"{base}/chat/completions"
    from omnimarket.handlers.model_provider_labels import MODEL_LABELS
    from omnimarket.nodes.node_onex_setup_effect.models.model_setup_provider_traits import (
        setup_provider_traits,
    )

    assert MODEL_LABELS.get(pid) == name
    picked = {p.provider: p.label for p in setup_provider_traits().picker_providers()}
    assert picked.get(pid) == name


def test_every_added_row_keeps_the_lists_rules_and_names_its_source() -> None:
    """C15."""
    from omnimarket.handlers.routing import byok_catalog_providers as m

    raw = yaml.safe_load(_ADDED.read_text(encoding="utf-8"))
    assert raw["schema_version"] == "byok_catalog_added_providers.v1"
    listed = yaml.safe_load(_LIST.read_text(encoding="utf-8"))["providers"]
    listed_ids = {row["id"] for row in listed}
    declared = {
        "gemini",
        "openai",
        "openrouter",
        "reflection",
        "anthropic",
        "glm",
        "zai",
        "local",
        "openai_compatible",
    }
    rows = raw["providers"]
    assert {row["id"] for row in rows} >= {"hopscotch"}
    for row in rows:
        assert set(row) == {"id", "name", "api", "doc", "source", "read_on"}, row
        assert m._fixed_https_base(row["api"]) == row["api"], row
        assert row["id"] not in listed_ids | declared, row
        assert not m._is_plan(row["id"], row["name"]), row
        assert row["source"].startswith("https://"), row
    loaded = m.load_byok_catalog_providers()
    assert {row["id"] for row in rows} <= set(loaded)


def test_a_provider_whose_address_does_not_work_is_offered_nowhere(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """C16."""
    from omnimarket.handlers.model_provider_labels import MODEL_LABELS
    from omnimarket.handlers.routing import byok_catalog_providers as m
    from omnimarket.nodes.node_onex_setup_effect.models.model_setup_provider_traits import (
        setup_provider_traits,
    )

    generated = m.load_generated_catalog_providers()
    offered = m.load_byok_catalog_providers()
    picked = {p.provider for p in setup_provider_traits().picker_providers()}
    for pid in _DEAD:
        assert pid in generated, pid  # models.dev still lists it
        assert pid not in offered, pid
        assert pid not in picked, pid
        assert pid not in MODEL_LABELS, pid
        assert resolve_byok_provider_backend(pid) is None, pid
    _arrange(monkeypatch, tmp_path)
    result = CliRunner().invoke(
        models_group, ["add", "llmtech", "--model", "m"], input="sk-test-0000\n"
    )
    assert result.exit_code != 0, result.output
    store = LocalByokCredentialStore()
    assert asyncio.run(store.get_secret("llm.llmtech.api_key")) is None


def test_every_withheld_row_says_why_and_is_still_listed() -> None:
    """C17."""
    from omnimarket.handlers.routing import byok_catalog_providers as m

    raw = yaml.safe_load(_WITHHELD.read_text(encoding="utf-8"))
    assert raw["schema_version"] == "byok_catalog_withheld_providers.v1"
    rows = raw["providers"]
    assert {row["provider"] for row in rows} == set(_DEAD)
    generated = m.load_generated_catalog_providers()
    for row in rows:
        assert set(row) == {"provider", "reason", "evidence", "checked_on"}, row
        assert row["provider"] in generated, row  # drop the row once it leaves
        assert len(row["reason"].split()) >= 2, row
        assert row["evidence"].strip(), row
    held = {w.provider for w in m.load_byok_catalog_withheld()}
    assert held == set(_DEAD)
