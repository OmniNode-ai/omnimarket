# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The model catalogue every onex screen shows (OMN-23413 AC21, AC27).

Failure modes:
  C1. models.dev overrides what the provider itself published (price, context).
  C2. A price no source states is shown as free ($0.00), or a capability no
      source states is shown as "no".
  C3. A failed refresh empties the table, or a body that is not JSON is cached.
  C4. A hosted price is applied to a model the user runs locally.
  C5. A provider, capability, column or status word is written into the code
      instead of read from configs/model_catalog.yaml or tui_design.yaml.
  C6. Prices lose precision through float arithmetic.
"""

from __future__ import annotations

import ast
import json
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from omnimarket.handlers import model_catalog
from omnimarket.handlers.model_catalog import (
    EMPTY_INDEX,
    ModelsDevIndex,
    catalog_config,
    catalog_entry,
    load_models_dev,
    stored_entries,
)

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)
_DOC = {
    "openrouter": {
        "models": {
            "vendor/big": {
                "name": "Big",
                "tool_call": True,
                "reasoning": False,
                "modalities": {"input": ["text", "image"]},
                "limit": {"context": 1_000_000, "output": 64_000},
                "cost": {"input": 5, "output": 25, "cache_read": 0.5, "tiers": [{}]},
            }
        }
    },
    "not-an-onex-provider": {"models": {"x": {"name": "X"}}},
}


def _index() -> ModelsDevIndex:
    return ModelsDevIndex(
        models=model_catalog._index(_DOC),
        fetched_at=_NOW.isoformat(),
        stale=False,
        error=None,
    )


def test_provider_values_win_and_models_dev_fills_only_the_gaps() -> None:
    """C1."""
    entry = catalog_entry(
        "openrouter",
        "vendor/big",
        deployment="cloud",
        index=_index(),
        provider_label="OpenRouter",
        context_tokens=200_000,
        input_usd_per_million=3,
        output_usd_per_million=15,
    )
    assert entry.context_tokens == 200_000
    assert entry.input_usd_per_million == Decimal(3)
    assert entry.sources == {
        "model": "OpenRouter",
        "context": "OpenRouter",
        "price": "OpenRouter",
        "capabilities": "models.dev",
    }
    assert entry.max_output_tokens == 64_000  # not stated by the provider
    assert entry.tiered_price is True


def test_models_dev_supplies_what_the_provider_did_not_state() -> None:
    entry = catalog_entry(
        "openrouter",
        "vendor/big",
        deployment="cloud",
        index=_index(),
        provider_label="OpenRouter",
    )
    assert entry.context_tokens == 1_000_000
    assert (entry.input_usd_per_million, entry.output_usd_per_million) == (
        Decimal(5),
        Decimal(25),
    )
    assert entry.sources["price"] == "models.dev"
    assert entry.capabilities["Tools"] == "yes"
    assert entry.capabilities["Reason"] == "no"
    assert entry.capabilities["Vision"] == "yes"
    assert entry.capabilities["Cache"] == "yes"


def test_nothing_stated_stays_unknown_never_free_or_no() -> None:
    """C2."""
    from omnimarket.nodes.node_onex_setup_effect.cli_model_table import price_text
    from omnimarket.nodes.node_onex_setup_effect.models.model_tui_design import (
        tui_design,
    )

    entry = catalog_entry(
        "openrouter",
        "vendor/unlisted",
        deployment="cloud",
        index=_index(),
        provider_label="OpenRouter",
    )
    assert entry.input_usd_per_million is None
    assert set(entry.capabilities.values()) == {"unknown"}
    assert price_text(entry) == tui_design().model_table.price_unknown
    assert "$0" not in price_text(entry)


def test_a_local_model_never_gets_a_hosted_price() -> None:
    """C4."""
    from omnimarket.nodes.node_onex_setup_effect.cli_model_table import price_text
    from omnimarket.nodes.node_onex_setup_effect.models.model_tui_design import (
        tui_design,
    )

    entry = catalog_entry(
        "openrouter",
        "vendor/big",
        deployment="local",
        index=_index(),
        provider_label="Server",
    )
    assert entry.input_usd_per_million is None
    assert price_text(entry) == tui_design().model_table.price_local


def test_prices_are_exact_decimals_and_print_in_dollars() -> None:
    """C6: 0.1 + 0.2 style drift cannot reach the table."""
    from omnimarket.nodes.node_onex_setup_effect.cli_model_table import price_text

    entry = catalog_entry(
        "openai",
        "m",
        deployment="cloud",
        index=EMPTY_INDEX,
        provider_label="OpenAI",
        input_usd_per_million=0.1,
        output_usd_per_million=0.0375,
    )
    assert entry.input_usd_per_million == Decimal("0.1")
    assert price_text(entry) == "$0.10 / $0.0375"


def _fetch_ok(url: str, timeout: float) -> bytes:
    return json.dumps(_DOC).encode()


def _fetch_down(url: str, timeout: float) -> bytes:
    raise OSError("network down")


def test_cache_is_used_while_fresh_and_a_failed_refresh_keeps_it_marked_stale(
    tmp_path: Path,
) -> None:
    """C3."""
    first = load_models_dev(tmp_path, fetch=_fetch_ok, now=_NOW)
    assert ("openrouter", "vendor/big") in first.models
    assert ("not-an-onex-provider", "x") not in first.models
    assert not first.stale

    def must_not_fetch(url: str, timeout: float) -> bytes:
        raise AssertionError("fresh cache was refetched")

    cache = tmp_path / str(catalog_config()["models_dev"]["cache_file"])
    os.utime(cache, (_NOW.timestamp(), _NOW.timestamp()))
    assert load_models_dev(tmp_path, fetch=must_not_fetch, now=_NOW).models

    later = _NOW + timedelta(
        hours=float(catalog_config()["models_dev"]["max_age_hours"]) + 1
    )
    stale = load_models_dev(tmp_path, fetch=_fetch_down, now=later)
    assert ("openrouter", "vendor/big") in stale.models
    assert stale.stale
    assert stale.error is not None


def test_no_cache_and_no_network_is_an_empty_index_not_an_error(tmp_path: Path) -> None:
    index = load_models_dev(tmp_path, fetch=_fetch_down, now=_NOW)
    assert index.models == {}
    assert index.error is not None


def test_a_body_that_is_not_json_is_never_cached(tmp_path: Path) -> None:
    load_models_dev(tmp_path, fetch=lambda _url, _timeout: b"<html>", now=_NOW)
    assert not (tmp_path / str(catalog_config()["models_dev"]["cache_file"])).exists()


def test_stored_entries_keep_the_users_order_and_test_status() -> None:
    rows = [
        {"provider": "gemini", "set_up": False, "models": ["ignored"]},
        {
            "provider": "openrouter",
            "set_up": True,
            "models": ["vendor/big", "vendor/other"],
            "last_test": {
                "model": "vendor/other",
                "status": "failed",
                "reason": "timeout",
            },
        },
    ]
    entries = stored_entries(
        rows,
        index=_index(),
        deployment_of=lambda _provider: "cloud",
        label_of=lambda provider, _server: provider.title(),
    )
    assert [(e.model_id, e.status, e.status_reason) for e in entries] == [
        ("vendor/big", "untested", None),
        ("vendor/other", "failed", "timeout"),
    ]


def _string_literals(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        )
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    }


def test_no_provider_capability_column_or_status_word_is_written_into_the_code() -> (
    None
):
    """C5: the lists live in data; the code that shows them names none of them."""
    from omnimarket.nodes.node_onex_setup_effect import cli_model_table
    from omnimarket.nodes.node_onex_setup_effect.models.model_tui_design import (
        tui_design,
    )

    table = tui_design().model_table
    config = catalog_config()
    data_words = {
        *config["provider_ids"],
        *config["provider_ids"].values(),
        *(str(spec["label"]) for spec in config["capabilities"]),
        *(column.title for column in table.columns),
        *(word.text for word in table.status.values()),
        *(word.text for word in table.deployment.values()),
        table.price_local,
        table.price_unknown,
        table.price_variable,
        table.search_placeholder,
        table.auto_label,
    }
    # A mark with no letter or digit (the unknown mark '-') is punctuation, not a word.
    data_words = {w for w in data_words if any(ch.isalnum() for ch in w)}
    data_words |= set(table.details.values())
    from omnimarket.nodes.node_onex_setup_effect import cli_tui

    words = tui_design().words
    data_words |= {text for text in words.values() if "{" not in text}
    data_words |= {spec.label for spec in tui_design().tabs}
    for module in (model_catalog, cli_model_table, cli_tui):
        literals = _string_literals(Path(str(module.__file__)))
        assert "mt-search" in literals or module is not cli_model_table  # reader works
        assert literals & data_words == set(), module.__name__


def test_the_literal_scan_catches_a_planted_word(tmp_path: Path) -> None:
    """Positive control for the scan above: a hard-coded word is found."""
    planted = tmp_path / "planted.py"
    planted.write_text('"""doc."""\nCOLUMNS = ["Tools", "Ready"]\n', encoding="utf-8")
    assert {"Tools", "Ready"} <= _string_literals(planted)
    assert "doc." not in _string_literals(planted)


def test_every_hosted_provider_config_offers_and_only_those_get_models_dev() -> None:
    """One provider list: /config's five (setup_provider_traits.yaml) decide it."""
    from omnimarket.nodes.node_onex_setup_effect.cli_tui import _hosted, _picker

    offered = {entry.provider for entry in _picker()}
    hosted = {provider for provider in offered if _hosted(provider)}
    assert set(catalog_config()["provider_ids"]) == hosted
