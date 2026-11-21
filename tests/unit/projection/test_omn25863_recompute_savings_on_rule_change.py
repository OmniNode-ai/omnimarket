# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-25863: a store recomputes its stored per-run savings whenever the savings rule or the price manifest changes.

Each local store keeps a per-run modelled saving that the savings view sums, computed once per store by the one-time
step omn24615_delegation_events_run_savings_recompute. omnimarket#6442 changed the rule (a run is priced by the model
that ran, model_name before delegated_to) with no new recompute, so a store that had recorded the step kept savings
priced by the alias, while the metering headline, folded live, used the new rule. A price manifest change leaves the
same gap. The recompute now runs as a store step named by a digest of the rule's version and every manifest price.

Failure modes, each with a test below:
  R1  a store that recorded the earlier recompute keeps a saving priced by the alias (the ticket's falsifier), for an
      alias the manifest does not list (R1) and for an alias the manifest prices differently from the model (R1b)
  R2  a price manifest change leaves stored savings at the old prices
  R3  the recompute runs on every open instead of once per change
  R4  the recorded cost columns are rewritten
  R5  a store that cannot be written fails to open instead of being read as it is
  R6  a reverted rule or price change is not recomputed (an earlier digest is still recorded)
  R7  a manifest that cannot be loaded breaks the open, or records a recompute against the empty fallback table

The model a run is priced by reaches its saving only through priced_spend: whether the manifest lists the model, and
whether it lists it free while the run cost money. Where the alias and the model are both paid, the two orders give the
same saving, so a case that can tell them apart needs an unlisted alias (R1) or a free one (R1b).
"""

from __future__ import annotations

import logging
import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_infra.models.pricing import (
    model_pricing_table as pricing_table_module,
)

from omnimarket import pricing
from omnimarket.nodes.node_projection_delegation.handlers import handler_run_savings
from omnimarket.projection import sqlite_database, sqlite_metering_summary
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter
from tests.unit.projection.test_sqlite_delegation_savings_view_omn20008 import (
    _delegation,
    _store,
)

pytestmark = pytest.mark.unit

_STALE = "9.999"


@pytest.fixture(autouse=True)
def _own_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The developer's ~/.onex/config.yaml can name another savings baseline; these tests use the default."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))


class _Manifest:
    """The real pricing manifest, read from a copy this test may edit: what both the store step and the saving read."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._original = pricing_table_module._DEFAULT_MANIFEST_PATH
        self._text = self._original.read_text(encoding="utf-8")
        self.path = tmp_path / "pricing_manifest.yaml"
        self._monkeypatch = monkeypatch
        self.restore()
        monkeypatch.setattr(pricing_table_module, "_DEFAULT_MANIFEST_PATH", self.path)

    def _reload(self) -> None:
        pricing._load_table.cache_clear()

    def restore(self) -> None:
        self.path.write_text(self._text, encoding="utf-8")
        self._reload()

    def reprice(
        self,
        model: str,
        factor: float,
        rates: tuple[str, ...] = ("input_cost_per_1k", "output_cost_per_1k"),
    ) -> None:
        """Multiply one model's rates (both, or the ones named), as a real price change would."""
        data = yaml.safe_load(self._text)
        entry = data["models"][model]
        for rate in rates:
            entry[rate] = entry[rate] * factor
        self.path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        self._reload()

    def remove(self) -> None:
        """The manifest cannot be found: pricing._load_table falls back to an empty table, resolve_baseline raises."""
        self.path.unlink()
        self._reload()


@pytest.fixture
def manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Manifest]:
    controlled = _Manifest(tmp_path, monkeypatch)
    try:
        yield controlled
    finally:
        monkeypatch.undo()
        pricing._load_table.cache_clear()


def _baseline_model() -> str:
    return sqlite_metering_summary.resolve_metering_baseline_model().model


def _recorded_rule_steps(path: Path) -> list[str]:
    with sqlite3.connect(path) as conn:
        return [
            row[0]
            for row in conn.execute(
                "SELECT step FROM omnimarket_sqlite_store_steps WHERE step LIKE 'omn25863_%' ORDER BY step"
            )
        ]


def _open(path: Path) -> None:
    SqliteDatabaseAdapter(path)._connect().close()


def _saving_and_cost(path: Path) -> tuple[Any, Any]:
    with sqlite3.connect(path) as conn:
        row = conn.execute(
            "SELECT modelled_savings_usd, cost_usd FROM delegation_events WHERE correlation_id = 'aliased'"
        ).fetchone()
    return row[0], row[1]


# The run is reached through an alias the pricing manifest does not list, while the model that ran is listed (free).
# Priced by the alias, as the earlier build did, priced_spend gives it no spend and the run no saving (NULL); priced
# by the model that ran, its $0 is a measured zero and it has a saving. The two rules diverge, so a store re-priced
# by the alias, or a writer reverted to alias-first, fails R1 (review of the first version of this test, 2026-11-19).
_MODEL = "qwen2.5-7b"
_ALIAS = "byok-unlisted-alias"


# R1b: the criterion's literal case, an alias the manifest prices differently from the model that ran. The model is
# paid and the alias is listed free, and the run cost money. Priced by the alias, a positive cost on a free model is a
# routing tier's flat rate, not a price, so the run had no spend and no saving; priced by the model that ran, its cost
# is spend and the run has a saving.
_PAID_MODEL = "gpt-4o-mini"
_FREE_ALIAS = "qwen2.5-7b"
_PAID_COST = 0.0003

_EARLIER_STEP = "omn24615_delegation_events_run_savings_recompute"


def _priced_by(path: Path, name_field: str) -> Any:
    """The run's saving when priced by one of its two names: model_name (the model that ran) or delegated_to (alias)."""
    columns = ["correlation_id", *handler_run_savings.RUN_SAVINGS_INPUT_COLUMNS]
    with sqlite3.connect(path) as conn:
        values = conn.execute(
            f"SELECT {', '.join(columns)} FROM delegation_events WHERE correlation_id = 'aliased'"
        ).fetchone()
    row = dict(zip(columns, values, strict=True))
    name = row[name_field]
    row["model_name"] = name
    row["delegated_to"] = name
    return handler_run_savings.modelled_run_savings_columns(row)["modelled_savings_usd"]


def _expected(path: Path) -> Any:
    """The saving this run gets when priced by the model that ran."""
    return _priced_by(path, "model_name")


def _earlier_store(
    tmp_path: Path,
    model: str = _MODEL,
    alias: str = _ALIAS,
    cost_usd: float = 0.0,
) -> Path:
    """A store an earlier build wrote: the earlier recompute recorded, the run's saving priced by its alias, and no
    rule-digest step recorded."""
    path = _store(
        tmp_path,
        (
            _delegation(
                "aliased",
                model_name=model,
                delegated_to=alias,
                cost_usd=cost_usd,
                tokens_input=2000,
                tokens_output=500,
            ),
        ),
    )
    alias_priced = _priced_by(path, "delegated_to")
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE delegation_events SET modelled_savings_usd = ? WHERE correlation_id = 'aliased'",
            (alias_priced,),
        )
        conn.execute(
            "DELETE FROM omnimarket_sqlite_store_steps WHERE step LIKE 'omn25863_%'"
        )
        earlier = conn.execute(
            "SELECT 1 FROM omnimarket_sqlite_store_steps WHERE step = ?",
            (_EARLIER_STEP,),
        ).fetchone()
    assert earlier is not None  # the store recorded the earlier recompute
    return path


def test_recompute_savings_a_store_with_the_earlier_recompute_reprices(
    tmp_path: Path,
) -> None:
    """R1: the ticket's falsifier. The alias-priced store held no saving; re-priced by the model, the run has one."""
    path = _earlier_store(tmp_path)
    expected = _expected(path)
    assert expected is not None  # the model-priced saving exists
    assert float(expected) > 0
    assert _saving_and_cost(path)[0] is None  # the alias-priced saving the store held
    _open(path)
    saving, _cost = _saving_and_cost(path)
    assert saving == expected


def test_recompute_savings_an_alias_priced_differently_from_its_model(
    tmp_path: Path,
) -> None:
    """R1b: the ticket's falsifier with an alias the manifest prices (free) differently from the model (paid)."""
    table = pricing._load_table()
    paid = table.get_entry(_PAID_MODEL)
    free = table.get_entry(_FREE_ALIAS)
    assert paid is not None
    assert paid.input_cost_per_1k > 0
    assert free is not None
    assert free.input_cost_per_1k == 0
    assert free.output_cost_per_1k == 0
    path = _earlier_store(
        tmp_path, model=_PAID_MODEL, alias=_FREE_ALIAS, cost_usd=_PAID_COST
    )
    expected = _expected(path)
    assert expected is not None
    assert _saving_and_cost(path)[0] is None  # priced by the free alias: no saving
    _open(path)
    assert _saving_and_cost(path)[0] == expected


@pytest.mark.parametrize("rate", ["input_cost_per_1k", "output_cost_per_1k"])
def test_recompute_savings_a_price_manifest_change_recomputes(
    tmp_path: Path, manifest: _Manifest, rate: str
) -> None:
    """R2: a real price change in the manifest (one of the baseline's rates doubled, the other left alone) re-prices
    the stored saving once more."""
    path = _earlier_store(
        tmp_path, model=_PAID_MODEL, alias=_FREE_ALIAS, cost_usd=_PAID_COST
    )
    _open(path)
    old_prices = _saving_and_cost(path)[0]
    assert old_prices == _expected(path)
    manifest.reprice(_baseline_model(), 2.0, rates=(rate,))
    new_prices = _expected(path)
    assert new_prices is not None
    assert new_prices != old_prices  # the change moves this run's saving
    _open(path)
    assert _saving_and_cost(path)[0] == new_prices


def test_recompute_savings_a_reverted_price_change_recomputes_again(
    tmp_path: Path, manifest: _Manifest
) -> None:
    """R6: prices A, then B, then back to A: the store recomputes at each change and ends priced by A again."""
    path = _earlier_store(
        tmp_path, model=_PAID_MODEL, alias=_FREE_ALIAS, cost_usd=_PAID_COST
    )
    _open(path)
    priced_by_a = _saving_and_cost(path)[0]
    manifest.reprice(_baseline_model(), 2.0)
    _open(path)
    priced_by_b = _saving_and_cost(path)[0]
    assert priced_by_b != priced_by_a
    manifest.restore()
    _open(path)
    assert _saving_and_cost(path)[0] == priced_by_a
    assert (
        len(_recorded_rule_steps(path)) == 1
    )  # only the current rule and prices stand recorded


def test_recompute_savings_a_manifest_that_cannot_load_leaves_the_store_as_it_is(
    tmp_path: Path, manifest: _Manifest
) -> None:
    """R7: with no manifest, the store still opens, keeps its savings and records no recompute for the empty table."""
    path = _earlier_store(
        tmp_path, model=_PAID_MODEL, alias=_FREE_ALIAS, cost_usd=_PAID_COST
    )
    _open(path)
    priced = _saving_and_cost(path)[0]
    assert priced is not None
    recorded = _recorded_rule_steps(path)
    manifest.remove()
    _open(path)
    assert _saving_and_cost(path)[0] == priced
    assert _recorded_rule_steps(path) == recorded
    manifest.restore()
    _open(path)
    assert _saving_and_cost(path)[0] == priced
    assert _recorded_rule_steps(path) == recorded


def test_recompute_savings_runs_once_per_change(tmp_path: Path) -> None:
    """R3: with nothing changed, a second open recomputes nothing."""
    path = _earlier_store(tmp_path)
    _open(path)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE delegation_events SET modelled_savings_usd = ? WHERE correlation_id = 'aliased'",
            (_STALE,),
        )
    _open(path)
    assert _saving_and_cost(path)[0] == _STALE


def test_recompute_savings_leaves_recorded_cost_alone(tmp_path: Path) -> None:
    """R4: decision D1 of OMN-24615, recorded cost columns are never rewritten, while the saving is."""
    path = _earlier_store(
        tmp_path, model=_PAID_MODEL, alias=_FREE_ALIAS, cost_usd=_PAID_COST
    )
    recorded = "SELECT cost_usd, cost_savings_usd FROM delegation_events WHERE correlation_id = 'aliased'"
    with sqlite3.connect(path) as conn:
        before = conn.execute(recorded).fetchone()
    assert before[0] == _PAID_COST
    _open(path)
    with sqlite3.connect(path) as conn:
        after = conn.execute(recorded).fetchone()
    assert _saving_and_cost(path)[0] == _expected(path)  # the recompute ran
    assert after == before


def test_recompute_savings_a_read_only_store_is_read_as_it_is(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """R5: a store in a read-only folder opens, keeps its saving, and the warning names the recompute it lacks."""
    path = _earlier_store(
        tmp_path, model=_PAID_MODEL, alias=_FREE_ALIAS, cost_usd=_PAID_COST
    )
    caplog.set_level(logging.WARNING, logger=sqlite_database.__name__)
    path.parent.chmod(0o555)
    try:
        if os.access(path.parent, os.W_OK):
            pytest.fail(
                f"{path.parent} is still writable with mode 0o555 (running as root?); "
                "this test cannot prove a read-only store is read"
            )
        rows = SqliteDatabaseAdapter(path).query("delegation_events")
    finally:
        path.parent.chmod(0o755)
    assert [row["correlation_id"] for row in rows] == ["aliased"]
    assert _saving_and_cost(path)[0] is None  # left as the earlier build wrote it
    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.name == sqlite_database.__name__ and record.levelno == logging.WARNING
    ]
    assert len(warnings) == 1
    assert str(path) in warnings[0]
    assert "recomputed each run's modelled saving" in warnings[0]
