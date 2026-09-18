# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex metering`` — read this install's own metering and savings (OMN-18697).

    onex metering                 # last 7 days
    onex metering --window today
    onex metering --window all --json

Goal row L7 asks that a customer read their per-call metering and the savings
figure off their own install without writing a query. Everything needed was
already on disk -- every local delegation writes a durable row carrying route,
model, token counts and cost -- but the only way to see a savings figure was a
hand SELECT, and the obvious hand SELECT was wrong (see
:mod:`omnimarket.projection.sqlite_metering_reader`). This command is the
surface.

THE DIVISION OF LABOUR IS THE DESIGN. This command reads and renders; it
computes nothing. The records come from the reader, the baseline price comes
from the canonical pricing manifest, and every figure comes from
``HandlerMeteringSummary``. A display that computes its own totals becomes a
second, divergent definition of the number the customer is judging us on.

WHAT IT WILL NOT PRINT. A savings figure without the baseline it was derived
against, and a zero standing in for an unmeasured run. Both are refused in the
summary model rather than avoided by convention here.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import click
from omnibase_infra.models.pricing.model_pricing_table import ModelPricingTable

from omnimarket.nodes.node_metering_summary_compute.handlers.handler_metering_summary import (
    HandlerMeteringSummary,
)
from omnimarket.nodes.node_metering_summary_compute.models.model_metering_summary import (
    ModelCounterfactualBaseline,
    ModelMeteringSummary,
    ModelMeteringSummaryRequest,
    ModelMeteringWindow,
)
from omnimarket.pricing import DEFAULT_BASELINE_MODEL
from omnimarket.projection.sqlite_metering_reader import (
    MeteringRecordsUnavailableError,
    default_metering_db_path,
    read_metering_records,
)

#: Window name to length. ``all`` has no start bound.
WINDOW_SPANS: dict[str, timedelta | None] = {
    "today": timedelta(days=1),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
    "all": None,
}


def resolve_baseline(model_id: str) -> ModelCounterfactualBaseline | None:
    """Pin the counterfactual price from the canonical pricing manifest.

    Returns ``None`` when the model is absent from the manifest. The caller
    must not substitute another model's price: a savings figure computed
    against a baseline nobody asked for is worse than no figure, because it
    looks like the one that was asked for.
    """
    table = ModelPricingTable.from_yaml()
    entry = table.get_entry(model_id)
    if entry is None:
        return None
    return ModelCounterfactualBaseline(
        model=model_id,
        price_in_per_1k=Decimal(str(entry.input_cost_per_1k)),
        price_out_per_1k=Decimal(str(entry.output_cost_per_1k)),
        as_of=entry.effective_date,
        pricing_manifest_version=table.schema_version,
        source="pricing_manifest",
    )


def _usd(value: Decimal | None, places: str = "0.0001") -> str:
    """Money for a human, or an explicit unknown. Never a stand-in zero."""
    if value is None:
        return "unknown"
    return f"${value.quantize(Decimal(places)):,}"


def render_text(summary: ModelMeteringSummary, db_path: Path) -> str:
    """Render the summary as the block a customer reads."""
    lines: list[str] = []
    window = summary.window
    start = window.start.isoformat() if window.start else "(all time)"
    lines.append(f"Local delegation metering — window {window.label}")
    lines.append(f"  from {start}")
    lines.append(f"  to   {window.end.isoformat()} (exclusive)")
    lines.append(f"  source {db_path}")
    lines.append("")

    lines.append(f"Runs                 {summary.runs_total:,}")
    lines.append(f"  fully measured     {summary.runs_measured:,}")
    lines.append(f"  unknown (tokens)   {summary.runs_unknown_tokens:,}")
    lines.append(f"  unknown (spend)    {summary.runs_unknown_spend:,}")
    lines.append(f"Tokens in            {summary.tokens_in:,}")
    lines.append(f"Tokens out           {summary.tokens_out:,}")
    lines.append("")

    lines.append(f"Spent                {_usd(summary.spend_usd)}")
    lines.append(f"Would have cost      {_usd(summary.counterfactual_usd)}")
    lines.append(f"Saved                {_usd(summary.savings_usd)}")

    if summary.baseline is not None:
        base = summary.baseline
        lines.append(
            f"  baseline           {base.model} "
            f"@ ${base.price_in_per_1k}/1k in, ${base.price_out_per_1k}/1k out"
        )
        lines.append(
            f"                     priced {base.as_of}, "
            f"{base.source} v{base.pricing_manifest_version}"
        )
    else:
        lines.append(
            "  baseline           UNRESOLVED — the baseline model is not in the "
            "pricing manifest, so no savings figure is computed"
        )
    lines.append(
        f"  measured over      {summary.runs_measured:,} of {summary.runs_total:,} runs"
    )
    lines.append("")

    recon = summary.reconciliation
    lines.append(
        f"Recorded savings column  {_usd(recon.recorded_savings_usd)} "
        f"over {recon.recorded_savings_runs:,} runs"
    )
    lines.append(
        "  The writer's own cost_savings_usd column. Its counterfactual baseline is not"
    )
    lines.append(
        "  persisted on the local row, so it is reported for reconciliation only and is"
    )
    lines.append("  not the figure above.")

    if summary.by_model:
        lines.append("")
        lines.append("By model")
        lines.append(
            f"  {'model':<40} {'runs':>8} {'measured':>9} {'tokens':>12} {'saved':>14}"
        )
        for row in summary.by_model:
            tokens = row.tokens_in + row.tokens_out
            lines.append(
                f"  {row.model[:40]:<40} {row.runs:>8,} {row.runs_measured:>9,} "
                f"{tokens:>12,} {_usd(row.savings_usd):>14}"
            )
    return "\n".join(lines)


@click.command("metering")
@click.option(
    "--window",
    type=click.Choice(sorted(WINDOW_SPANS), case_sensitive=False),
    default="7d",
    show_default=True,
    help="How far back to summarise.",
)
@click.option(
    "--baseline",
    default=DEFAULT_BASELINE_MODEL,
    show_default=True,
    help="Model whose price the savings are counted against.",
)
@click.option(
    "--db",
    "db_path",
    type=click.Path(path_type=Path),
    default=None,
    help="Local delegation evidence database. Defaults to the canonical path.",
)
@click.option("--top", default=10, show_default=True, help="Models to break out.")
@click.option("--json", "as_json", is_flag=True, help="Emit the summary as JSON.")
def metering_command(
    window: str,
    baseline: str,
    db_path: Path | None,
    top: int,
    as_json: bool,
) -> None:
    """Read this install's own delegation metering and savings.

    Every figure comes from the durable per-call records this machine already
    writes. A run that recorded no measurement is counted as unknown, never as
    a zero, and a savings figure is never printed without the counterfactual
    baseline it was derived against.
    """
    resolved_db = db_path if db_path is not None else default_metering_db_path()
    now = datetime.now(tz=UTC)
    span = WINDOW_SPANS[window.lower()]
    start = None if span is None else now - span

    try:
        records = read_metering_records(
            db_path=resolved_db, window_start=start, window_end=now
        )
    except MeteringRecordsUnavailableError as exc:
        raise click.ClickException(
            f"{exc}\n"
            "No metering can be reported from an unreadable evidence store. "
            "Run `onex delegate` once to create it."
        ) from exc

    summary = HandlerMeteringSummary().handle(
        ModelMeteringSummaryRequest(
            window=ModelMeteringWindow(label=window.lower(), start=start, end=now),
            records=records,
            baseline=resolve_baseline(baseline),
            top_models=top,
        )
    )

    if as_json:
        click.echo(json.dumps(summary.model_dump(mode="json"), indent=2))
        return

    click.echo(render_text(summary, resolved_db))


if __name__ == "__main__":  # pragma: no cover - module entry for doubted installs
    # Runnable as `python -m omnimarket.cli.cli_metering` as well as through the
    # `onex metering` entry point. A console script only appears after the
    # package is reinstalled, and the host where you want this answer is often
    # precisely the host whose installed build you are doubting.
    metering_command()
