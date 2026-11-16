# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The dashboard's money figures, computed here so the page only displays them.

The page used to add and divide served money itself (the Overview window sum,
Spend by model, route spend and cost per run). Every one of those figures is
computed in this module now, as exact decimals, and served by the dashboard API:

* :func:`manifest_run_cost` prices one run from the pricing manifest: the
  manifest price for the model that served the final answer, times the run's
  recorded tokens. It sits BESIDE the recorded ``cost_usd`` (the tier or BYOK
  spend priced at call time), which is never replaced. A model with no manifest
  row reads ``None`` with a reason, never a guessed price.
* :func:`window_totals` sums the current baseline's metering day rows over a
  window of UTC days. A day with no row ran nothing and adds zero; a day row
  missing a value makes that figure ``None`` (unknown, not zero).
* :func:`usage_aggregates` serves spend per model, spend per route and cost per
  run (measured cost over measured calls) from the usage rows in the window.

Money is served as decimal strings so nothing downstream adds floats. This
module reads no store and holds no SQL: the caller hands it rows the projection
read node already returned for this install's tenant.
"""

from __future__ import annotations

import functools
import math
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import Final

from omnibase_infra.models.pricing.model_pricing_table import ModelPricingTable

#: A window whose summed days carry more than one pricing manifest version.
MIXED_PRICING: Final = "mixed basis"
#: A route or model the usage row does not name.
NOT_RECORDED: Final = "not_recorded"
UNKNOWN_MODEL: Final = "Unknown model"
#: The longest window the API sums, in days.
MAX_WINDOW_DAYS: Final = 366
#: Places a quotient keeps; the page's formatter truncates to cents.
_QUOTIENT_PLACES: Final = Decimal("1e-12")

_RANGE_MONEY: Final = ("spend_usd", "savings_usd", "counterfactual_usd")
_RANGE_COUNTS: Final = (
    "runs_total",
    "runs_measured",
    "runs_unknown_tokens",
    "runs_unknown_spend",
    "tokens_in",
    "tokens_out",
)

#: The run columns, in order, that name the model which served the answer. An
#: escalated run's ``routed_model`` is the model that answered last; the
#: requested ``delegated_to`` and ``model_name`` stand in only when it is absent.
_SERVED_MODEL_COLUMNS: Final = ("routed_model", "delegated_to", "model_name")


# --------------------------------------------------------------------------- #
# Values
# --------------------------------------------------------------------------- #


def _decimal(value: object) -> Decimal | None:
    """A served number as an exact decimal; ``None`` for anything else."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if not isinstance(value, (int, float, str, Decimal)):
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = Decimal(text)
    except InvalidOperation:
        return None
    return parsed if parsed.is_finite() else None


def _count(value: object) -> int | None:
    parsed = _decimal(value)
    if parsed is None or parsed != parsed.to_integral_value():
        return None
    return int(parsed)


def usd(value: Decimal | None) -> str | None:
    """A decimal as the served money string, never in exponent form."""
    if value is None:
        return None
    if value == 0:
        return "0"
    return format(value, "f")


def _divide(amount: Decimal | None, calls: int | None) -> str | None:
    if amount is None or calls is None or calls <= 0:
        return None
    quotient = (amount / Decimal(calls)).quantize(
        _QUOTIENT_PLACES, rounding=ROUND_HALF_EVEN
    )
    return usd(quotient.normalize())


def _sum_present(values: Iterable[object]) -> Decimal | None:
    """The sum of the values that are numbers; ``None`` when none is."""
    present = [d for d in (_decimal(v) for v in values) if d is not None]
    return sum(present, Decimal(0)) if present else None


def _missing(value: object) -> bool:
    return value is None or value == ""


# --------------------------------------------------------------------------- #
# Run cost from the pricing manifest
# --------------------------------------------------------------------------- #


@functools.lru_cache(maxsize=1)
def load_pricing_manifest() -> ModelPricingTable | None:
    """The packaged pricing manifest, read once; ``None`` when it cannot load."""
    try:
        return ModelPricingTable.from_yaml()
    except Exception:
        return None


def _served_model(row: Mapping[str, object]) -> str | None:
    for column in _SERVED_MODEL_COLUMNS:
        value = row.get(column)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def manifest_run_cost(
    row: Mapping[str, object], table: ModelPricingTable | None
) -> dict[str, object]:
    """The run's manifest-priced cost, its basis, and why it is null when it is.

    ``manifest_cost_reason`` is ``priced`` with a figure, else one of
    ``no_served_model``, ``tokens_not_recorded`` (both counts zero or absent,
    the legacy storage default), ``manifest_unavailable`` or
    ``model_not_in_manifest``. The model id is looked up exactly: no alias, no
    nearest model, no fallback price.
    """
    model = _served_model(row)
    result: dict[str, object] = {
        "manifest_cost_usd": None,
        "manifest_cost_model": model,
        "manifest_cost_pricing_manifest_version": None,
        "manifest_cost_reason": "priced",
    }
    if model is None:
        return {**result, "manifest_cost_reason": "no_served_model"}
    tokens_in = _count(row.get("tokens_input")) or 0
    tokens_out = _count(row.get("tokens_output")) or 0
    if tokens_in <= 0 and tokens_out <= 0:
        return {**result, "manifest_cost_reason": "tokens_not_recorded"}
    if table is None:
        return {**result, "manifest_cost_reason": "manifest_unavailable"}
    entry = table.get_entry(model)
    if entry is None:
        return {**result, "manifest_cost_reason": "model_not_in_manifest"}
    cost = (
        Decimal(tokens_in) * Decimal(str(entry.input_cost_per_1k))
        + Decimal(tokens_out) * Decimal(str(entry.output_cost_per_1k))
    ) / 1000
    return {
        **result,
        "manifest_cost_usd": format(cost, "f"),
        "manifest_cost_pricing_manifest_version": table.schema_version,
    }


def manifest_run_costs(
    rows: Sequence[Mapping[str, object]],
) -> dict[str, dict[str, object]]:
    """Each run's manifest-priced cost, keyed by the run's ``correlation_id``.

    Served beside the rows rather than inside them, so a served row keeps
    exactly the columns its exposure declares. A row that serves no token
    counts, or no ``correlation_id``, is left out: it cannot be priced, and an
    entry of nulls would claim it was tried.
    """
    table = load_pricing_manifest()
    out: dict[str, dict[str, object]] = {}
    for row in rows:
        key = row.get("correlation_id")
        if key is None or not ("tokens_input" in row or "tokens_output" in row):
            continue
        out[str(key)] = manifest_run_cost(row, table)
    return out


# --------------------------------------------------------------------------- #
# Windows
# --------------------------------------------------------------------------- #


class WindowError(ValueError):
    """The requested window is not two ISO dates, in order, within the limit."""


def parse_window(start: str, end: str) -> list[str]:
    """The window's UTC days, inclusive, as ``YYYY-MM-DD`` strings."""
    try:
        first = date.fromisoformat(start)
        last = date.fromisoformat(end)
    except ValueError as exc:
        raise WindowError("start and end must be YYYY-MM-DD dates") from exc
    if len(start) != 10 or len(end) != 10:
        raise WindowError("start and end must be YYYY-MM-DD dates")
    if last < first:
        raise WindowError("end is before start")
    span = (last - first).days + 1
    if span > MAX_WINDOW_DAYS:
        raise WindowError(f"a window is at most {MAX_WINDOW_DAYS} days")
    return [(first + timedelta(days=i)).isoformat() for i in range(span)]


def _as_of(row: Mapping[str, object]) -> str:
    value = row.get("as_of")
    return value if isinstance(value, str) else ""


def window_totals(
    rows: Sequence[Mapping[str, object]], days: Sequence[str]
) -> dict[str, object] | None:
    """The current baseline's metering day rows summed over ``days``.

    ``None`` when no metering row was served at all: nothing was read, so the
    window is not measured. The baseline, its state and the as-of come from the
    latest all-time row. The window's price basis is the versions of the days
    actually summed: one version is named, several (or a version beside an
    unversioned day) read :data:`MIXED_PRICING`, and a window with no day rows
    keeps the all-time row's basis.
    """
    all_rows = [r for r in rows if r.get("window_kind") == "all"]
    day_rows = [r for r in rows if r.get("window_kind") == "day"]
    if not all_rows and not day_rows:
        return None
    anchor = max(all_rows or day_rows, key=_as_of)
    by_day: dict[str, Mapping[str, object]] = {}
    for row in day_rows:
        start = row.get("window_start")
        if row.get("baseline_model") != anchor.get("baseline_model"):
            continue
        if not isinstance(start, str) or not start:
            continue
        key = start[:10]
        held = by_day.get(key)
        if held is None or _as_of(row) > _as_of(held):
            by_day[key] = row
    summed = [by_day[d] for d in days if d in by_day]
    versions = {
        str(r["pricing_manifest_version"])
        for r in summed
        if not _missing(r.get("pricing_manifest_version"))
    }
    unversioned = any(_missing(r.get("pricing_manifest_version")) for r in summed)
    if not summed:
        pricing: object = anchor.get("pricing_manifest_version")
    elif len(versions) > 1 or (len(versions) == 1 and unversioned):
        pricing = MIXED_PRICING
    else:
        pricing = next(iter(versions)) if versions else None
    totals: dict[str, object] = {
        "window_kind": "day" if len(days) == 1 else "range",
        "window_start": days[0],
        "window_end": days[-1],
        "as_of": anchor.get("as_of"),
        "baseline_model": anchor.get("baseline_model"),
        "pricing_manifest_version": pricing,
        "baseline_state": anchor.get("baseline_state"),
        "days_without_runs": len(days) - len(summed),
    }
    for field in _RANGE_MONEY:
        values = [_decimal(r.get(field)) for r in summed]
        if any(v is None for v in values):
            totals[field] = None
        else:
            totals[field] = usd(sum((v for v in values if v is not None), Decimal(0)))
    for field in _RANGE_COUNTS:
        counts = [_count(r.get(field)) for r in summed]
        totals[field] = (
            None
            if any(c is None for c in counts)
            else sum(c for c in counts if c is not None)
        )
    return totals


# --------------------------------------------------------------------------- #
# Usage: per model, per route, per day, cost per run
# --------------------------------------------------------------------------- #


def _call_counts(rows: Sequence[Mapping[str, object]]) -> tuple[int | None, int | None]:
    """(measured calls, unmeasured calls); ``None`` where a row leaves it unknown."""
    pairs = [
        (_count(r.get("call_count")), _count(r.get("unmeasured_call_count")))
        for r in rows
    ]
    known = bool(pairs) and all(
        total is not None and missing is not None and 0 <= missing <= total
        for total, missing in pairs
    )
    measured = sum((t or 0) - (m or 0) for t, m in pairs) if known else None
    unmeasured = (
        sum(m or 0 for _, m in pairs)
        if pairs and all(m is not None for _, m in pairs)
        else None
    )
    return measured, unmeasured


def _all_known(values: Sequence[int | None]) -> int | None:
    if not values or any(v is None for v in values):
        return None
    return sum(v for v in values if v is not None)


def _spend_group(rows: Sequence[Mapping[str, object]]) -> dict[str, object]:
    measured_cost = _sum_present(r.get("measured_cost_usd") for r in rows)
    measured_calls, unmeasured_calls = _call_counts(rows)
    return {
        "calls": _all_known([_count(r.get("call_count")) for r in rows]),
        "measured_cost_usd": usd(measured_cost),
        "measured_calls": measured_calls,
        "unmeasured_calls": unmeasured_calls,
        "cost_per_run_usd": _divide(measured_cost, measured_calls),
    }


def _name(value: object, default: str) -> str:
    return value if isinstance(value, str) and value else default


def usage_aggregates(
    rows: Sequence[Mapping[str, object]], days: Sequence[str]
) -> dict[str, object]:
    """Spend per model, per route and per day, and cost per run, over ``days``.

    Spend is measured spend only (``measured_cost_usd``); a group with no
    measured call reads ``None``, never ``0``. Cost per run is measured spend
    over measured calls.
    """
    window = set(days)
    usage = [
        r
        for r in rows
        if isinstance(r.get("usage_day"), str) and str(r["usage_day"])[:10] in window
    ]
    by_model: dict[str, list[Mapping[str, object]]] = {}
    by_route: dict[str, list[Mapping[str, object]]] = {}
    by_day: dict[str, list[Mapping[str, object]]] = {}
    for row in usage:
        by_model.setdefault(_name(row.get("model_id"), UNKNOWN_MODEL), []).append(row)
        by_route.setdefault(_name(row.get("cost_tier_name"), NOT_RECORDED), []).append(
            row
        )
        by_day.setdefault(str(row["usage_day"])[:10], []).append(row)
    total_calls = _all_known(
        [_count(r.get("call_count")) for group in by_model.values() for r in group]
    )
    models: list[dict[str, object]] = []
    for model, group in by_model.items():
        spend = _spend_group(group)
        calls = spend["calls"]
        share = calls / total_calls if isinstance(calls, int) and total_calls else None
        models.append(
            {
                "model": model,
                "routes": sorted(
                    {_name(r.get("cost_tier_name"), NOT_RECORDED) for r in group}
                ),
                **spend,
                "call_share": share,
                "input_tokens": _all_known(
                    [_count(r.get("input_tokens")) for r in group]
                ),
                "output_tokens": _all_known(
                    [_count(r.get("output_tokens")) for r in group]
                ),
            }
        )
    routes = [
        {"route": route, **_spend_group(group)} for route, group in by_route.items()
    ]
    per_day = [{"day": day, **_spend_group(by_day.get(day, []))} for day in days]
    model_spend = _sum_present(m["measured_cost_usd"] for m in models)
    overall = _spend_group(usage)
    return {
        "models": models,
        "model_spend_total_usd": usd(model_spend),
        "routes": routes,
        "days": per_day,
        "measured_cost_usd": overall["measured_cost_usd"],
        "measured_calls": overall["measured_calls"],
        "unmeasured_calls": overall["unmeasured_calls"],
        "cost_per_run_usd": overall["cost_per_run_usd"],
    }


__all__ = [
    "MAX_WINDOW_DAYS",
    "MIXED_PRICING",
    "NOT_RECORDED",
    "UNKNOWN_MODEL",
    "WindowError",
    "load_pricing_manifest",
    "manifest_run_cost",
    "manifest_run_costs",
    "parse_window",
    "usage_aggregates",
    "usd",
    "window_totals",
]
