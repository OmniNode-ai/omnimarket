# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerMeteringSummary — local metering and savings, computed once (OMN-18697).

Pure and deterministic: the caller reads the records and pins the baseline,
this handler does the arithmetic and the classification. That split is what
lets every rule below be tested against a window that has already happened,
with no database, no pricing manifest and no network.

THE THREE RULES, IN THE ORDER THEY DECIDE THINGS.

1. **Classify before summing.** Every run lands in exactly one measurement
   class first, and only :attr:`EnumMeteringMeasurement.MEASURED` runs enter
   the savings subtraction. The alternative — summing a column and dividing by
   the row count — is what made the earlier hand query report a figure whose
   denominator nobody had checked. The local table declares
   ``cost_savings_usd NOT NULL DEFAULT 0.0``, so an unmeasured run and a
   genuinely free one are identical bytes; classification is the only place
   that distinction can be recovered.

2. **No baseline, no figure.** When the caller could not pin the baseline
   model in the pricing manifest, ``savings_usd`` and ``counterfactual_usd``
   are ``None`` and ``baseline_state`` is UNRESOLVED. Not zero, not a guess,
   not the previous manifest's price. The summary model refuses to be
   constructed any other way.

3. **Decimal throughout.** Prices are per-1,000-token rates multiplied by
   integer token counts, and a float there accumulates error across tens of
   thousands of rows until the headline disagrees with its own recomputation.
   The output is exact and a reader can reproduce it from the printed baseline.

WHAT THIS DELIBERATELY DOES NOT DO. It does not fall back to a second baseline
model when the first is missing, and it does not estimate tokens from prompt
length. Both would produce a plausible number in exactly the situation where
the honest answer is that the record does not say.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from omnimarket.nodes.node_metering_summary_compute.models.model_metering_summary import (
    EnumBaselineState,
    ModelCounterfactualBaseline,
    ModelMeteringModelRow,
    ModelMeteringReconciliation,
    ModelMeteringSummary,
    ModelMeteringSummaryRequest,
)

_PER_1K = Decimal("1000")


def counterfactual_cost_usd(
    *,
    baseline: ModelCounterfactualBaseline,
    tokens_in: int,
    tokens_out: int,
) -> Decimal:
    """What ``tokens_in``/``tokens_out`` would have cost at ``baseline``.

    Exposed by name because it is the one line a reader has to be able to
    recompute by hand from the printed baseline in order to audit the headline.
    """
    return (
        baseline.price_in_per_1k * Decimal(tokens_in)
        + baseline.price_out_per_1k * Decimal(tokens_out)
    ) / _PER_1K


class _ModelAccumulator:
    """Mutable per-model tally, collapsed into a frozen row at the end."""

    __slots__ = ("runs", "runs_measured", "savings", "spend", "tokens_in", "tokens_out")

    def __init__(self) -> None:
        self.runs = 0
        self.runs_measured = 0
        self.tokens_in = 0
        self.tokens_out = 0
        self.spend = Decimal("0")
        self.savings = Decimal("0")


class HandlerMeteringSummary:
    """Summarise local delegation records into one metering projection."""

    def handle(self, payload: ModelMeteringSummaryRequest) -> ModelMeteringSummary:
        baseline = payload.baseline

        runs_measured = 0
        runs_unknown_tokens = 0
        runs_unknown_spend = 0
        tokens_in_total = 0
        tokens_out_total = 0
        spend_total = Decimal("0")
        counterfactual_total = Decimal("0")

        recorded_savings_total = Decimal("0")
        recorded_savings_runs = 0

        per_model: dict[str, _ModelAccumulator] = defaultdict(_ModelAccumulator)

        for record in payload.records:
            bucket = per_model[record.model or "(unrecorded)"]
            bucket.runs += 1

            if record.recorded_savings_usd is not None:
                recorded_savings_total += record.recorded_savings_usd
                recorded_savings_runs += 1

            # Token counts are summed wherever they exist, including on runs
            # whose spend is unknown: "how much work went through the local
            # path" is answerable even where "what it saved" is not.
            tokens_in = record.tokens_in or 0
            tokens_out = record.tokens_out or 0
            if record.has_tokens:
                tokens_in_total += tokens_in
                tokens_out_total += tokens_out
                bucket.tokens_in += tokens_in
                bucket.tokens_out += tokens_out

            # The two guards below reproduce EnumMeteringMeasurement's rules in
            # the order they narrow the type. The enum on the record is the
            # readable statement of the same classification and is what the
            # tests assert against; these are the branches that consume it.
            if not record.has_tokens:
                runs_unknown_tokens += 1
                continue
            spend = record.spend_usd
            if spend is None:
                runs_unknown_spend += 1
                continue

            runs_measured += 1
            bucket.runs_measured += 1
            spend_total += spend
            bucket.spend += spend

            if baseline is not None:
                run_counterfactual = counterfactual_cost_usd(
                    baseline=baseline, tokens_in=tokens_in, tokens_out=tokens_out
                )
                counterfactual_total += run_counterfactual
                bucket.savings += run_counterfactual - spend

        has_measured = runs_measured > 0
        spend_usd = spend_total if has_measured else None
        if baseline is not None and has_measured:
            counterfactual_usd: Decimal | None = counterfactual_total
            savings_usd: Decimal | None = counterfactual_total - spend_total
        else:
            counterfactual_usd = None
            savings_usd = None

        return ModelMeteringSummary(
            window=payload.window,
            runs_total=len(payload.records),
            runs_measured=runs_measured,
            runs_unknown_tokens=runs_unknown_tokens,
            runs_unknown_spend=runs_unknown_spend,
            tokens_in=tokens_in_total,
            tokens_out=tokens_out_total,
            spend_usd=spend_usd,
            counterfactual_usd=counterfactual_usd,
            savings_usd=savings_usd,
            baseline_state=(
                EnumBaselineState.UNRESOLVED
                if baseline is None
                else EnumBaselineState.RESOLVED
            ),
            baseline=baseline,
            reconciliation=ModelMeteringReconciliation(
                recorded_savings_usd=recorded_savings_total,
                recorded_savings_runs=recorded_savings_runs,
                recorded_savings_baseline_available=False,
            ),
            by_model=self._top_models(
                per_model, payload.top_models, baselined=baseline is not None
            ),
        )

    @staticmethod
    def _top_models(
        per_model: dict[str, _ModelAccumulator],
        limit: int,
        *,
        baselined: bool,
    ) -> tuple[ModelMeteringModelRow, ...]:
        """Break out the busiest models, ties broken by name for determinism."""
        ordered = sorted(per_model.items(), key=lambda kv: (-kv[1].runs, kv[0]))
        rows: list[ModelMeteringModelRow] = []
        for model, acc in ordered[:limit]:
            measured = acc.runs_measured > 0
            rows.append(
                ModelMeteringModelRow(
                    model=model,
                    runs=acc.runs,
                    runs_measured=acc.runs_measured,
                    tokens_in=acc.tokens_in,
                    tokens_out=acc.tokens_out,
                    spend_usd=acc.spend if measured else None,
                    savings_usd=acc.savings if (measured and baselined) else None,
                )
            )
        return tuple(rows)
