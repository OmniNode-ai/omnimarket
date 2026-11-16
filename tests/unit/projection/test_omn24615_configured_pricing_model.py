# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24615 AC2: a local run's saving is priced against the configured baseline.

The audit found the baseline fixed in code (claude-sonnet-5-5): every local
caller resolved it with an empty overlay, so a pricing.baseline_model set in
the ONEX home's config.yaml was never read, and the savings topic served a NULL
baseline. With no overlay in hand, the resolver now reads that block, and the
topic names the newest run's priced baseline.

Failure modes, each with a test below:
  B1  the headline is folded under the code default, not the configured model
  B2  a run's modelled saving names the code default
  B3  the savings topic serves a NULL baseline when its newest run is unpriced
  B4  no config file, no pricing block, or a broken file stops resolution
  B5  an overlay the caller passes no longer wins over the local config
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from omnimarket.nodes.node_projection_delegation.handlers.handler_run_savings import (
    modelled_run_savings_columns,
)
from omnimarket.pricing import DEFAULT_BASELINE_MODEL, resolve_baseline_model
from omnimarket.projection.sqlite_metering_summary import (
    refresh_metering_summary,
    resolve_metering_baseline_model,
)
from tests.unit.projection.test_sqlite_delegation_savings_view_omn20008 import (
    _TENANT,
    _call,
    _delegation,
    _row,
    _store,
)

pytestmark = pytest.mark.unit

_CONFIGURED = "claude-opus-4-6"
_NOW = datetime(2026, 11, 16, tzinfo=UTC)


def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config: str | None) -> None:
    home = tmp_path / "home"
    (home / ".onex").mkdir(parents=True)
    if config is not None:
        (home / ".onex" / "config.yaml").write_text(config, encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))


def _configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _home(tmp_path, monkeypatch, f"pricing:\n  baseline_model: {_CONFIGURED}\n")


def _headline_savings(path: Path) -> tuple[str, str | None]:
    model = resolve_metering_baseline_model().model
    rows = refresh_metering_summary(path, _TENANT, model, _NOW)
    [row] = [r for r in rows if r.window_kind == "all"]
    return row.baseline_model, row.savings_usd


def test_configured_baseline_prices_and_names_the_headline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B1: the AC2 falsifier."""
    runs = (_delegation("measured"),)
    calls = (_call("measured", "measured"),)
    _home(tmp_path / "default", monkeypatch, None)
    default_model, default_savings = _headline_savings(
        _store(tmp_path / "a", runs, calls)
    )
    _configured(tmp_path / "configured", monkeypatch)
    model, savings = _headline_savings(_store(tmp_path / "b", runs, calls))
    assert default_model == DEFAULT_BASELINE_MODEL
    assert model == _CONFIGURED
    assert savings is not None
    assert default_savings is not None
    assert savings != default_savings


def test_configured_baseline_names_each_runs_modelled_saving(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B2."""
    _configured(tmp_path, monkeypatch)
    columns = modelled_run_savings_columns(_delegation("measured"))
    assert columns["savings_baseline_model"] == _CONFIGURED
    assert columns["modelled_savings_usd"] is not None


def test_configured_baseline_the_topic_names_the_newest_priced_run(
    tmp_path: Path,
) -> None:
    """B3."""
    path = _store(
        tmp_path,
        (
            _delegation(
                "priced",
                savings_baseline_model=_CONFIGURED,
                savings_pricing_manifest_version="1.0.0",
            ),
            _delegation(
                "unpriced",
                created_at="2026-10-11T09:00:00+00:00",
                tokens_input=0,
                tokens_output=0,
                modelled_savings_usd=None,
            ),
        ),
        (_call("priced", "measured"), _call("unpriced", "measured")),
    )
    row = _row(path)
    assert row["baseline_model"] == _CONFIGURED
    assert row["pricing_manifest_version"] == "1.0.0"


@pytest.mark.parametrize(
    "config",
    [None, "lanes: {}\n", "pricing: [not, a, mapping]\n", "pricing: {baseline_model: "],
    ids=["no-file", "no-pricing-block", "pricing-not-a-mapping", "broken-yaml"],
)
def test_configured_baseline_falls_back_to_the_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config: str | None
) -> None:
    """B4."""
    _home(tmp_path, monkeypatch, config)
    assert resolve_metering_baseline_model().model == DEFAULT_BASELINE_MODEL


def test_configured_baseline_a_passed_overlay_still_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B5."""
    _configured(tmp_path, monkeypatch)
    selection = resolve_baseline_model(
        overlay={"pricing": {"baseline_model": "gpt-4o"}}, store={}
    )
    assert selection.model == "gpt-4o"
    assert selection.selection_case == "overlay"
