# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Continue a stored metering window from its watermark (OMN-21288).

A stored row's ``summary_json`` is the canonical summary of every run the row
folded, and the ``all`` row's ``as_of`` is one microsecond past the newest of
them. So the next refresh needs only the runs created since: this module folds
those through the canonical :class:`HandlerMeteringSummary` and adds the result
to the stored summary. The sum of two summaries is the summary of the union of
their runs (every figure is a count, a sum or a per-model tally), so the row an
incremental history leaves is byte-identical to the row a rebuild writes.

Pure and synchronous: the caller hands it to a worker thread, because folding
is CPU work that must not run on the loop that serves the projection API.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from omnimarket.nodes.node_metering_summary_compute import (
    EnumBaselineState,
    HandlerMeteringSummary,
    ModelCounterfactualBaseline,
    ModelMeteringModelRow,
    ModelMeteringReconciliation,
    ModelMeteringRecord,
    ModelMeteringSummary,
    ModelMeteringSummaryRequest,
    ModelMeteringWindow,
)
from omnimarket.nodes.node_projection_metering_summary.handlers.handler_projection_metering_summary import (
    summary_row,
)
from omnimarket.nodes.node_projection_metering_summary.models import (
    ModelMeteringSummaryRow,
)

_ZERO = Decimal("0")


@dataclass(frozen=True)
class ModelStoredContinuation:
    """What a tenant's stored rows give a refresh to continue from."""

    watermark: datetime
    all_summary: ModelMeteringSummary


def stored_continuation(
    all_row: Mapping[str, Any] | None,
    *,
    baseline: ModelCounterfactualBaseline | None,
) -> ModelStoredContinuation | None:
    """The stored ``all`` row as a point to continue from, or None to fold whole.

    None when there is no row, when it was folded at another pricing manifest or
    baseline, or when it cannot be read back: a stored figure that cannot be
    trusted is rebuilt, never extended.
    """
    if all_row is None:
        return None
    version = baseline.pricing_manifest_version if baseline is not None else None
    if all_row["pricing_manifest_version"] != version:
        return None
    try:
        summary = ModelMeteringSummary.model_validate_json(all_row["summary_json"])
        watermark = datetime.fromisoformat(str(all_row["as_of"]))
    except ValueError:
        return None
    if summary.baseline != baseline or watermark.tzinfo is None:
        return None
    return ModelStoredContinuation(
        watermark=watermark.astimezone(UTC), all_summary=summary
    )


def _sum_optional(left: Decimal | None, right: Decimal | None) -> Decimal:
    return (left or _ZERO) + (right or _ZERO)


def _merge_models(
    base: Sequence[ModelMeteringModelRow],
    delta: Sequence[ModelMeteringModelRow],
    *,
    baselined: bool,
) -> tuple[ModelMeteringModelRow, ...]:
    merged: dict[str, ModelMeteringModelRow] = {}
    for row in (*base, *delta):
        held = merged.get(row.model)
        if held is None:
            merged[row.model] = row
            continue
        runs_measured = held.runs_measured + row.runs_measured
        merged[row.model] = ModelMeteringModelRow(
            model=row.model,
            backends=tuple(sorted({*held.backends, *row.backends})),
            runs=held.runs + row.runs,
            runs_measured=runs_measured,
            tokens_in=held.tokens_in + row.tokens_in,
            tokens_out=held.tokens_out + row.tokens_out,
            spend_usd=_sum_optional(held.spend_usd, row.spend_usd)
            if runs_measured
            else None,
            savings_usd=_sum_optional(held.savings_usd, row.savings_usd)
            if runs_measured and baselined
            else None,
        )
    ordered = sorted(merged.values(), key=lambda m: (-m.runs, m.model))
    return tuple(ordered)


def merge_summaries(
    base: ModelMeteringSummary | None,
    delta: ModelMeteringSummary,
    *,
    window: ModelMeteringWindow,
) -> ModelMeteringSummary:
    """The summary of ``base``'s runs and ``delta``'s runs together."""
    if base is None:
        return delta.model_copy(update={"window": window})
    baseline = delta.baseline
    runs_measured = base.runs_measured + delta.runs_measured
    spend_total = _sum_optional(base.spend_usd, delta.spend_usd)
    counterfactual_total = _sum_optional(
        base.counterfactual_usd, delta.counterfactual_usd
    )
    has_measured = runs_measured > 0
    resolved = baseline is not None
    return ModelMeteringSummary(
        window=window,
        runs_total=base.runs_total + delta.runs_total,
        runs_measured=runs_measured,
        runs_unknown_tokens=base.runs_unknown_tokens + delta.runs_unknown_tokens,
        runs_unknown_spend=base.runs_unknown_spend + delta.runs_unknown_spend,
        runs_failed=base.runs_failed + delta.runs_failed,
        tokens_in=base.tokens_in + delta.tokens_in,
        tokens_out=base.tokens_out + delta.tokens_out,
        spend_usd=spend_total if has_measured else None,
        counterfactual_usd=counterfactual_total if resolved and has_measured else None,
        savings_usd=counterfactual_total - spend_total
        if resolved and has_measured
        else None,
        baseline_state=EnumBaselineState.RESOLVED
        if resolved
        else EnumBaselineState.UNRESOLVED,
        baseline=baseline,
        reconciliation=ModelMeteringReconciliation(
            recorded_savings_usd=base.reconciliation.recorded_savings_usd
            + delta.reconciliation.recorded_savings_usd,
            recorded_savings_runs=base.reconciliation.recorded_savings_runs
            + delta.reconciliation.recorded_savings_runs,
            recorded_savings_baseline_available=False,
        ),
        by_model=_merge_models(base.by_model, delta.by_model, baselined=resolved),
    )


def _fold(
    records: Sequence[ModelMeteringRecord],
    window: ModelMeteringWindow,
    baseline: ModelCounterfactualBaseline | None,
) -> ModelMeteringSummary:
    return HandlerMeteringSummary().handle(
        ModelMeteringSummaryRequest(
            window=window,
            records=tuple(records),
            baseline=baseline,
            top_models=max(1, len(records)),
        )
    )


@dataclass
class ModelMeteringFoldState:
    """The folded windows of one tenant while a refresh adds runs to them.

    Starts from the stored rows (an incremental refresh) or empty (a rebuild);
    :func:`add_records` takes any number of chunks, so the memory a refresh
    holds is the chunk and the windows, never every run of the tenant.
    """

    as_of: datetime | None = None
    all_summary: ModelMeteringSummary | None = None
    days: dict[date, ModelMeteringSummary] = field(default_factory=dict)
    touched: set[date] = field(default_factory=set)
    records_folded: int = 0


def state_from_continuation(
    continuation: ModelStoredContinuation,
) -> ModelMeteringFoldState:
    """The state an incremental refresh continues: the stored all row."""
    return ModelMeteringFoldState(
        as_of=continuation.watermark, all_summary=continuation.all_summary
    )


def merge_stored_days(
    state: ModelMeteringFoldState, stored_days: Mapping[str, str]
) -> None:
    """Add the stored day rows' summaries beneath the days the new runs touched."""
    for key, text in stored_days.items():
        day = date.fromisoformat(key)
        if day in state.touched:
            state.days[day] = merge_summaries(
                ModelMeteringSummary.model_validate_json(text),
                state.days[day],
                window=_day_window(day),
            )


def add_records(
    state: ModelMeteringFoldState,
    records: Sequence[ModelMeteringRecord],
    *,
    baseline: ModelCounterfactualBaseline | None,
) -> None:
    """Fold ``records`` through the canonical handler and add them to ``state``."""
    if not records:
        return
    newest = max(r.occurred_at.astimezone(UTC) for r in records) + timedelta(
        microseconds=1
    )
    state.as_of = newest if state.as_of is None else max(state.as_of, newest)
    state.records_folded += len(records)
    by_day: dict[date, list[ModelMeteringRecord]] = {}
    for record in records:
        by_day.setdefault(record.occurred_at.astimezone(UTC).date(), []).append(record)
    all_window = ModelMeteringWindow(label="all", start=None, end=state.as_of)
    state.all_summary = merge_summaries(
        state.all_summary, _fold(records, all_window, baseline), window=all_window
    )
    for day, day_records in by_day.items():
        window = _day_window(day)
        state.days[day] = merge_summaries(
            state.days.get(day), _fold(day_records, window, baseline), window=window
        )
        state.touched.add(day)


def _day_window(day: date) -> ModelMeteringWindow:
    start = datetime.combine(day, time.min, tzinfo=UTC)
    return ModelMeteringWindow(label="day", start=start, end=start + timedelta(days=1))


def state_rows(
    state: ModelMeteringFoldState,
    *,
    tenant_id: str,
    baseline: ModelCounterfactualBaseline | None,
    baseline_model: str,
) -> tuple[ModelMeteringSummaryRow, ...]:
    """The all row and the touched days' rows, in the order a whole fold writes them."""
    if state.as_of is None or state.all_summary is None:
        return ()
    all_window = ModelMeteringWindow(label="all", start=None, end=state.as_of)
    rows = [
        summary_row(
            state.all_summary.model_copy(update={"window": all_window}),
            tenant_id=tenant_id,
            baseline_model=baseline_model,
            baseline=baseline,
            kind="all",
            key="",
            as_of=state.as_of,
        )
    ]
    rows.extend(
        summary_row(
            state.days[day],
            tenant_id=tenant_id,
            baseline_model=baseline_model,
            baseline=baseline,
            kind="day",
            key=day.isoformat(),
            as_of=state.as_of,
        )
        for day in sorted(state.touched)
    )
    return tuple(rows)


__all__: list[str] = [
    "ModelMeteringFoldState",
    "ModelStoredContinuation",
    "add_records",
    "merge_stored_days",
    "merge_summaries",
    "state_from_continuation",
    "state_rows",
    "stored_continuation",
]
