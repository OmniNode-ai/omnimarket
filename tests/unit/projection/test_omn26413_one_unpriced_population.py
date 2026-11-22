# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26413: the metering headline and the two local views count unpriced runs over the same runs.

OMN-25862 gave the three one per-run rule (priced_spend). They still counted over different runs. The headline counts a run
as unknown spend only if:
  - it is a real run (not a fixture or experiment);
  - it was not returned to the caller;
  - it recorded tokens;
  - its call usage was measured;
  - it is the tenant's own.
The savings view and model-routing applied none of these filters (routing left out experiment rows only).

Failure modes, each a case below (spec records/plans/OMN-26413/spec.md, approved 2026-11-22):
  P1-P7  the ticket's run-population cases: no tokens, estimated usage, unknown usage, no call row while the store has
         others, returned to the caller, a fixture row, an experiment row (AC3, which supersedes AC1; the
         second-tenant case moves to a follow-up ticket, D2-SHAPE-26413 = A)
  P9     a NULL data_source run stays unpriced in all three (regression guard for the views' new filter)
  R1-R16 the per-run rule cases agree (AC2), and each fails on the deliberate break of the view's rule mapped to it
  O1     over-correction: every served field of both views other than the unpriced count is unchanged
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from omnimarket.models.model_projection_read import ModelProjectionReadRequest
from omnimarket.nodes.node_projection_read_effect.handlers.handler_projection_read import (
    HandlerProjectionRead,
)
from omnimarket.nodes.node_projection_read_effect.ports.sqlite_row_source import (
    SqliteTableRowSource,
)
from omnimarket.projection.sqlite_metering_summary import (
    read_summary_row,
    refresh_metering_after_terminal,
    resolve_metering_baseline_model,
)
from tests.unit.projection.test_omn24615_evaluated_runs_and_served_model import (
    _ROUTING,
)
from tests.unit.projection.test_omn24615_evaluated_runs_and_served_model import (
    _TOPICS as _ROUTING_TOPICS,
)
from tests.unit.projection.test_sqlite_delegation_savings_view_omn20008 import (
    _TENANT,
    _call,
    _delegation,
    _row,
    _store,
)

pytestmark = pytest.mark.unit

_TERMINAL = datetime(2026, 10, 11, tzinfo=UTC)
_UNLISTED = "vendor/unlisted-model"
_PRICED = "gpt-4o-mini"
_FREE = "qwen2.5-7b"
_SAVINGS_VIEW = "projection_delegation_savings"
_ROUTING_VIEW = "projection_delegation_model_routing"


def _run(
    cid: str, model: str | None, cost: float | None, **fields: Any
) -> dict[str, Any]:
    return _delegation(
        cid, model_name=model, delegated_to=model, cost_usd=cost, **fields
    )


def _headline(path: Path, tenant: str = _TENANT) -> int:
    """runs_unknown_spend of the all row the delegation path's own refresh stores."""
    model = resolve_metering_baseline_model().model
    refresh_metering_after_terminal(
        path, tenant_id=tenant, terminal_at=_TERMINAL, baseline_model=model
    )
    row = read_summary_row(path, tenant, "all", "", model)
    assert row is not None
    return int(row.runs_unknown_spend)


def _routing_row(path: Path, tenant: str = _TENANT) -> dict[str, Any]:
    handler = HandlerProjectionRead(
        topic_map=_ROUTING_TOPICS, row_source=SqliteTableRowSource(path)
    )
    result = asyncio.run(
        handler.handle(ModelProjectionReadRequest(topic=_ROUTING, tenant_id=tenant))
    )
    assert result.http_status == 200, result.response
    [routing] = (result.response or {})["rows"]
    return dict(routing)


def _unmeasured(routing: dict[str, Any]) -> int:
    return sum(
        int(model["unmeasured_cost_count"])
        for tier in routing["by_tier"]["tiers"]
        for model in tier["models"]
    )


def _counts(path: Path) -> tuple[int, int, int]:
    """(headline runs_unknown_spend, savings unpriced_call_count, routing unmeasured_cost_count summed)."""
    savings = int(_row(path)["unpriced_call_count"])
    return _headline(path), savings, _unmeasured(_routing_row(path))


def _priced(cid: str = "priced") -> dict[str, Any]:
    return _run(cid, _PRICED, 0.0001)


def _measured(*runs: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    return tuple(_call(str(r["correlation_id"]), "measured") for r in runs)


# ---------------------------------------------------------------------------
# AC3: the ticket's first seven run-population cases, plus the NULL data_source guard.
# Each store is one priced run of the tenant plus one $0 run on an unlisted model, changed as named.
# ---------------------------------------------------------------------------


def _population_store(tmp_path: Path, case: str) -> Path:
    priced = _priced()
    zero = _run("case", _UNLISTED, 0.0)
    calls: tuple[dict[str, Any], ...]
    if case == "no_tokens":
        zero.update(tokens_input=0, tokens_output=0)
        calls = _measured(priced, zero)
    elif case in ("estimated_usage", "unknown_usage"):
        calls = (*_measured(priced), _call("case", case.split("_")[0]))
    elif case == "no_call_row":
        calls = _measured(priced)
    elif case == "returned_to_caller":
        zero["terminal_failure_cause"] = "returned_to_caller"
        calls = _measured(priced, zero)
    elif case in ("fixture", "experiment"):
        zero["data_source"] = case
        calls = _measured(priced, zero)
    elif case == "null_data_source":
        zero["data_source"] = None
        calls = _measured(priced, zero)
    else:
        raise AssertionError(case)
    return _store(tmp_path, (priced, zero), calls)


_POPULATION = {
    # case: the unpriced count all three must serve
    "no_tokens": 0,
    "estimated_usage": 0,
    "unknown_usage": 0,
    "no_call_row": 0,
    "returned_to_caller": 0,
    "fixture": 0,
    "experiment": 0,
    "null_data_source": 1,
}


@pytest.mark.parametrize("case", sorted(_POPULATION))
def test_the_three_unpriced_counts_cover_the_same_runs(
    tmp_path: Path, case: str
) -> None:
    """P1-P10: one store, three readings, one count."""
    path = _population_store(tmp_path, case)
    expected = _POPULATION[case]
    assert _counts(path) == (expected, expected, expected)


# ---------------------------------------------------------------------------
# AC2: the 16 per-run rule cases. Each store is one priced run plus the case's run, every run with a measured call
# (the no-call-rows case has none at all).
# ---------------------------------------------------------------------------

_RULE: dict[str, tuple[dict[str, Any], int]] = {
    # case: (fields of the case's run, the unpriced count all three must serve)
    "null_cost_listed": (
        {"model_name": _PRICED, "delegated_to": _PRICED, "cost_usd": None},
        1,
    ),
    "null_cost_unlisted": (
        {"model_name": _UNLISTED, "delegated_to": _UNLISTED, "cost_usd": None},
        1,
    ),
    "unlisted_zero": (
        {"model_name": _UNLISTED, "delegated_to": _UNLISTED, "cost_usd": 0.0},
        1,
    ),
    "unlisted_positive": (
        {"model_name": _UNLISTED, "delegated_to": _UNLISTED, "cost_usd": 0.01},
        1,
    ),
    "unlisted_negative": (
        {"model_name": _UNLISTED, "delegated_to": _UNLISTED, "cost_usd": -0.01},
        1,
    ),
    "listed_priced_zero": (
        {"model_name": _PRICED, "delegated_to": _PRICED, "cost_usd": 0.0},
        0,
    ),
    "listed_priced_positive": (
        {"model_name": _PRICED, "delegated_to": _PRICED, "cost_usd": 0.02},
        0,
    ),
    "listed_free_zero": (
        {"model_name": _FREE, "delegated_to": _FREE, "cost_usd": 0.0},
        0,
    ),
    "listed_free_positive": (
        {"model_name": _FREE, "delegated_to": _FREE, "cost_usd": 0.01},
        1,
    ),
    "upper_case_name": (
        {
            "model_name": "GPT-4O-MINI",
            "delegated_to": "GPT-4O-MINI",
            "cost_usd": 0.0001,
        },
        0,
    ),
    "model_name_empty_delegated_listed": (
        {"model_name": "", "delegated_to": _PRICED, "cost_usd": 0.0001},
        0,
    ),
    "model_name_null_delegated_listed": (
        {"model_name": None, "delegated_to": _PRICED, "cost_usd": 0.0001},
        0,
    ),
    "model_unlisted_delegated_listed": (
        {"model_name": _UNLISTED, "delegated_to": _PRICED, "cost_usd": 0.0},
        1,
    ),
    "model_listed_delegated_unlisted": (
        {"model_name": _PRICED, "delegated_to": _UNLISTED, "cost_usd": 0.0001},
        0,
    ),
    "both_empty": ({"model_name": "", "delegated_to": "", "cost_usd": 0.0}, 1),
    "untiered_unlisted_zero": (
        {
            "model_name": _UNLISTED,
            "delegated_to": _UNLISTED,
            "cost_usd": 0.0,
            "cost_tier_name": None,
        },
        1,
    ),
    "no_call_rows": (
        {"model_name": _UNLISTED, "delegated_to": _UNLISTED, "cost_usd": 0.0},
        1,
    ),
}


def _rule_store(tmp_path: Path, case: str, *, base: bool = True) -> Path:
    fields, _ = _RULE[case]
    run = _delegation("case", **fields)
    runs = (_priced(), run) if base else (run,)
    calls = () if case == "no_call_rows" else _measured(*runs)
    return _store(tmp_path, runs, calls)


@pytest.mark.parametrize("case", sorted(_RULE))
def test_the_three_unpriced_counts_agree_on_every_rule_case(
    tmp_path: Path, case: str
) -> None:
    """R1-R16: the per-run rule gives one count in all three readings."""
    expected = _RULE[case][1]
    assert _counts(_rule_store(tmp_path, case)) == (expected, expected, expected)


# The deliberate breaks. Each mutant rewrites one clause of a view's unpriced rule in the store's own view text (so it
# breaks whichever revision the store runs), for one view at a time. Patterns cover both views' table aliases.
_ALIAS = r"(?:events|de)"
_MUTANTS: dict[str, tuple[str, str]] = {
    # name: (pattern, replacement)
    "unlisted_is_priced": (
        r"COALESCE\(pm_exact\.free, pm_lower\.free\) IS NULL\s+OR",
        "0 OR",
    ),
    "listed_is_unpriced": (
        r"COALESCE\(pm_exact\.free, pm_lower\.free\) IS NULL\s+OR",
        "COALESCE(pm_exact.free, pm_lower.free) IS NOT NULL OR",
    ),
    "free_positive_is_priced": (
        rf"\({_ALIAS}\.cost_usd > 0 AND COALESCE\(pm_exact\.free, pm_lower\.free\) = 1\)",
        "0",
    ),
    "no_lower_case_key": (r"pm_lower\.model_key = lower\(", "pm_lower.model_key = ("),
    "no_delegated_to_fallback": (
        rf"COALESCE\(NULLIF\(({_ALIAS})\.model_name, ''\), NULLIF\({_ALIAS}\.delegated_to, ''\), ''\)",
        r"COALESCE(\1.model_name, '')",
    ),
    "null_cost_is_priced": (
        rf"THEN NULL\s+ELSE ({_ALIAS})\.cost_usd END",
        r"THEN NULL ELSE COALESCE(\1.cost_usd, 0) END",
    ),
    "never_unpriced": (
        r"THEN 1 ELSE 0 END(\), 0\) AS unmeasured_cost_count| AS unpriced)",
        r"THEN 0 ELSE 0 END\1",
    ),
}

# Which mutant proves each rule case (AC2: each case shown failing on a deliberate break of the view's rule).
_KILLS: dict[str, str] = {
    "null_cost_listed": "null_cost_is_priced",
    "null_cost_unlisted": "never_unpriced",
    "unlisted_zero": "unlisted_is_priced",
    "unlisted_positive": "unlisted_is_priced",
    "unlisted_negative": "unlisted_is_priced",
    "listed_priced_zero": "listed_is_unpriced",
    "listed_priced_positive": "listed_is_unpriced",
    "listed_free_zero": "listed_is_unpriced",
    "listed_free_positive": "free_positive_is_priced",
    "upper_case_name": "no_lower_case_key",
    "model_name_empty_delegated_listed": "no_delegated_to_fallback",
    "model_name_null_delegated_listed": "no_delegated_to_fallback",
    "model_unlisted_delegated_listed": "unlisted_is_priced",
    "model_listed_delegated_unlisted": "listed_is_unpriced",
    "both_empty": "unlisted_is_priced",
    "untiered_unlisted_zero": "unlisted_is_priced",
    "no_call_rows": "unlisted_is_priced",
}


def _mutate(path: Path, view: str, pattern: str, replacement: str) -> None:
    """Replace the store's view with a copy whose rule is broken; refuse a pattern that matches nothing."""
    conn = sqlite3.connect(path)
    try:
        [(sql,)] = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'view' AND name = ?", (view,)
        ).fetchall()
        broken, hits = re.subn(pattern, replacement, sql)
        assert hits > 0, f"mutant pattern {pattern!r} matches nothing in {view}"
        conn.execute(f"DROP VIEW {view}")
        conn.execute(broken)
        conn.commit()
    finally:
        conn.close()


def test_every_rule_case_has_a_mutant() -> None:
    """The mapping covers every AC2 case, so no case is proven by nothing."""
    assert set(_KILLS) == set(_RULE)
    assert set(_KILLS.values()) <= set(_MUTANTS)


@pytest.mark.parametrize("view", [_SAVINGS_VIEW, _ROUTING_VIEW])
@pytest.mark.parametrize("case", sorted(_RULE))
def test_each_rule_case_fails_on_its_deliberate_break(
    tmp_path: Path, case: str, view: str
) -> None:
    """Positive control for R1-R16: with the view's rule broken, the three counts no longer agree.

    The listed-model mutant would also flip the base priced run, so those cases run without it: the break must be
    caught on the case's own row.
    """
    path = _rule_store(tmp_path, case, base=_KILLS[case] != "listed_is_unpriced")
    _mutate(path, view, *_MUTANTS[_KILLS[case]])
    headline, savings, routing = _counts(path)
    assert not (headline == savings == routing), (
        case,
        view,
        headline,
        savings,
        routing,
    )


# The population filter's own breaks: each removes one of the five conditions the views now share with the headline.
_POPULATION_MUTANTS: dict[str, tuple[str, str, str]] = {
    # case: (pattern, replacement, why)
    "fixture": (
        rf"\({_ALIAS}\.data_source IS NOT 'fixture'\)",
        "(1 = 1)",
        "fixture filter",
    ),
    "experiment": (
        rf"{_ALIAS}\.data_source IS NOT 'experiment'",
        "1 = 1",
        "experiment filter",
    ),
    "returned_to_caller": (
        rf"\({_ALIAS}\.terminal_failure_cause IS NOT 'returned_to_caller'\)",
        "(1 = 1)",
        "returned-to-caller filter",
    ),
    "no_tokens": (
        rf"\(CAST\(COALESCE\({_ALIAS}\.tokens_input, 0\) AS INTEGER\) <> 0"
        rf" OR CAST\(COALESCE\({_ALIAS}\.tokens_output, 0\) AS INTEGER\) <> 0\)",
        "(1 = 1)",
        "token filter",
    ),
    "estimated_usage": (
        r"'unrecorded' ELSE 'measured' END\) = 'measured'",
        "'unrecorded' ELSE 'measured' END) IS NOT NULL",
        "call-usage filter",
    ),
    "unknown_usage": (
        r"'unrecorded' ELSE 'measured' END\) = 'measured'",
        "'unrecorded' ELSE 'measured' END) IS NOT NULL",
        "call-usage filter",
    ),
    "no_call_row": (
        r"'unrecorded' ELSE 'measured' END\) = 'measured'",
        "'unrecorded' ELSE 'measured' END) IS NOT NULL",
        "call-usage filter",
    ),
}


@pytest.mark.parametrize("view", [_SAVINGS_VIEW, _ROUTING_VIEW])
@pytest.mark.parametrize("case", sorted(_POPULATION_MUTANTS))
def test_each_population_case_fails_when_its_filter_is_removed(
    tmp_path: Path, case: str, view: str
) -> None:
    """Positive control for P1-P7: drop the view's matching filter and the counts disagree again."""
    path = _population_store(tmp_path, case)
    pattern, replacement, _why = _POPULATION_MUTANTS[case]
    _mutate(path, view, pattern, replacement)
    headline, savings, routing = _counts(path)
    assert not (headline == savings == routing), (
        case,
        view,
        headline,
        savings,
        routing,
    )


# ---------------------------------------------------------------------------
# O1: over-correction. Each view, served beside the revision it replaces (OMN-25862's), differs only in its count.
# ---------------------------------------------------------------------------

_SQL = Path(__file__).resolve().parents[3] / "src/omnimarket/nodes"
_PREVIOUS = {
    _SAVINGS_VIEW: _SQL
    / "node_projection_savings/sqlite/delegation_savings_view_one_unpriced_rule.sql",
    _ROUTING_VIEW: _SQL
    / "node_projection_delegation/sqlite/delegation_model_routing_view_v5.sql",
}
_COUNT_FIELD = {
    _SAVINGS_VIEW: "unpriced_call_count",
    _ROUTING_VIEW: "unmeasured_cost_count",
}


def _without(value: Any, field: str) -> Any:
    if isinstance(value, dict):
        return {k: _without(v, field) for k, v in value.items() if k != field}
    if isinstance(value, list):
        return [_without(v, field) for v in value]
    return value


def _replace_view(path: Path, view: str, sql: str) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(f"DROP VIEW {view}")
        conn.execute(sql)
        conn.commit()
    finally:
        conn.close()


_READERS: dict[str, Callable[[Path], dict[str, Any]]] = {
    _SAVINGS_VIEW: _row,
    _ROUTING_VIEW: _routing_row,
}


@pytest.mark.parametrize("view", [_SAVINGS_VIEW, _ROUTING_VIEW])
@pytest.mark.parametrize("case", sorted({*_POPULATION, *_RULE}))
def test_only_the_unpriced_count_differs_from_the_previous_revision(
    tmp_path: Path, case: str, view: str
) -> None:
    path = (
        _population_store(tmp_path, case)
        if case in _POPULATION
        else _rule_store(tmp_path, case)
    )
    read = _READERS[view]
    current = _without(read(path), _COUNT_FIELD[view])
    previous_sql = re.sub(
        r"(?s)^.*?(CREATE VIEW)", r"\1", _PREVIOUS[view].read_text("utf-8")
    )
    _replace_view(path, view, previous_sql)
    assert _without(read(path), _COUNT_FIELD[view]) == current
