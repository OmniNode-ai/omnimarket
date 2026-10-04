# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Render durable metering snapshots (OMN-19977).

Windows: default ``all``, ``--window today`` (UTC), or ``--day YYYY-MM-DD``.
JSON is the full stored ``summary_json`` object with four additional top-level
key fields: ``tenant_id``, ``window_kind``, ``window_start``, ``baseline_model``.
Money remains decimal strings or null. JSON is never truncated by ``--top``.
Snapshots refresh only when absent or their pinned baseline differs from the
current resolution; new delegations alone do not invalidate a stored snapshot.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal

import click

from omnimarket.local_deployment.tenant_identity import (
    LocalTenantIdentityError,
    require_local_tenant_identity,
)
from omnimarket.nodes.node_metering_summary_compute.models.model_metering_summary import (
    ModelMeteringSummary,
)
from omnimarket.pricing import DEFAULT_BASELINE_MODEL
from omnimarket.projection.sqlite_metering_reader import (
    MeteringRecordsUnavailableError,
    default_metering_db_path,
)
from omnimarket.projection.sqlite_metering_summary import (
    read_summary_row,
    refresh_metering_summary,
)

WINDOWS = ("all", "today")


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
    type=click.Choice(WINDOWS, case_sensitive=False),
    default="all",
    show_default=True,
    help="Stored window: all time or the current UTC calendar day.",
)
@click.option(
    "--day",
    type=click.DateTime(formats=["%Y-%m-%d"]),
    default=None,
    help="UTC calendar day, YYYY-MM-DD.",
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
@click.option(
    "--top",
    type=click.IntRange(min=0),
    default=10,
    show_default=True,
    help="Models to break out.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit the summary as JSON.")
@click.option(
    "--include-fixtures",
    is_flag=True,
    help="Also count rows the dev seed wrote (data_source=fixture). Off by default.",
)
def metering_command(
    window: str,
    day: datetime | None,
    baseline: str,
    db_path: Path | None,
    top: int,
    as_json: bool,
    include_fixtures: bool,
) -> None:
    """Read this install's own delegation metering and savings.

    Every figure comes from the durable per-call records this machine already
    writes. A run that recorded no measurement is counted as unknown, never as
    a zero, and a savings figure is never printed without the counterfactual
    baseline it was derived against.
    """
    resolved_db = db_path if db_path is not None else default_metering_db_path()
    now = datetime.now(tz=UTC)
    if day is not None and window.lower() != "all":
        raise click.UsageError("--day cannot be combined with --window today")
    selected_day: date | None = (
        day.date() if day else (now.date() if window.lower() == "today" else None)
    )
    kind: Literal["day", "all"] = "day" if selected_day else "all"
    start = selected_day.isoformat() if selected_day else ""
    try:
        if not resolved_db.exists():
            raise MeteringRecordsUnavailableError(
                f"no local delegation evidence database at {resolved_db}"
            )
        # OMN-17427: the summary row is keyed on this install's own minted
        # tenant identity (`onex local init`), never a literal. A literal here
        # wrote every row under "local", a tenant no install ever minted.
        tenant_id = str(require_local_tenant_identity(db_path=resolved_db))
        # Always refresh through the node's own fold before reading its row:
        # the local evidence store can gain runs at any time, and a stored row
        # older than the newest run would print a savings figure that omits it.
        refresh_metering_summary(
            resolved_db,
            tenant_id,
            baseline,
            now,
            days=frozenset({selected_day}) if selected_day else None,
            include_fixtures=include_fixtures,
        )
        row = read_summary_row(resolved_db, tenant_id, kind, start, baseline)
    except MeteringRecordsUnavailableError as exc:
        raise click.ClickException(
            f"{exc}\nNo metering can be reported from an unreadable evidence store. "
            "Run `onex delegate` once to create it."
        ) from exc
    except LocalTenantIdentityError as exc:
        raise click.ClickException(str(exc)) from exc
    if row is None:
        raise click.ClickException("Metering refresh did not produce the requested row")
    if as_json:
        payload = json.loads(row.summary_json)
        payload.update(
            {
                key: getattr(row, key)
                for key in (
                    "tenant_id",
                    "window_kind",
                    "window_start",
                    "baseline_model",
                    "as_of",
                    "pricing_manifest_version",
                )
            }
        )
        click.echo(json.dumps(payload, sort_keys=True))
        return
    summary = ModelMeteringSummary.model_validate_json(row.summary_json)
    summary = summary.model_copy(update={"by_model": summary.by_model[:top]})
    click.echo(render_text(summary, resolved_db))


if __name__ == "__main__":  # pragma: no cover - module entry for doubted installs
    # Runnable as `python -m omnimarket.cli.cli_metering` as well as through the
    # `onex metering` entry point. A console script only appears after the
    # package is reinstalled, and the host where you want this answer is often
    # precisely the host whose installed build you are doubting.
    metering_command()
