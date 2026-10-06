# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Render the stored metering row (OMN-19977).

Windows: default ``all``, ``--window today`` (UTC), or ``--day YYYY-MM-DD``.
The baseline is the one the delegation resolved (``resolve_baseline_model``),
unless ``--baseline`` names another stored row.

JSON is the stored row: every column of ``metering-summary.v1`` except
``summary_json``, whose keys are printed at the top level. Money remains
decimal strings or null. JSON is never truncated by ``--top``.

The end of every ``onex delegate`` refreshes the rows. This command only reads
them: it never folds, writes or prices. A row that is absent, or older than a
recorded run, or priced against another manifest version is answered with a
typed state naming the repair (exit 1), never recomputed.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from functools import cache
from pathlib import Path
from typing import Any, Literal, NoReturn

import click
import yaml

from omnimarket.local_deployment.tenant_identity import (
    LocalTenantIdentityError,
    require_local_tenant_identity,
)
from omnimarket.models.model_metering_row_refusal import (
    METERING_ROW_REPAIR,
    EnumMeteringRowState,
    ModelMeteringRowRefusal,
)
from omnimarket.nodes.node_metering_summary_compute.models.model_metering_summary import (
    EnumBaselineState,
    ModelMeteringSummary,
)
from omnimarket.nodes.node_projection_read_effect.handlers.handler_projection_read import (
    HandlerProjectionRead,
)
from omnimarket.nodes.node_projection_read_effect.models import (
    ModelProjectionReadRequest,
)
from omnimarket.nodes.node_projection_read_effect.ports.sqlite_row_source import (
    SqliteTableRowSource,
)
from omnimarket.projection.discovery import load_projection_exposures_from_contract
from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.sqlite_metering_reader import (
    MeteringRecordsUnavailableError,
    default_metering_db_path,
    read_metering_records,
)
from omnimarket.projection.sqlite_metering_summary import (
    current_pricing_manifest_version,
    resolve_metering_baseline_model,
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
    lines.append(
        f"  unknown (tokens)   {summary.runs_unknown_tokens:,}"
        "   counted here, in no sum below"
    )
    lines.append(
        f"  unknown (spend)    {summary.runs_unknown_spend:,}"
        "   counted here, tokens summed, in no money sum"
    )
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
            "  baseline           BASELINE_UNRESOLVED — the baseline model is not "
            "in the pricing manifest, so no savings figure is computed"
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


_METERING_CONTRACT = (
    Path(__file__).resolve().parents[1]
    / "nodes"
    / "node_projection_metering_summary"
    / "contract.yaml"
)
# The read node refuses a store without the table; for this command that is a
# store no delegation has refreshed yet, not an unreadable one.
_TABLE_MISSING = "projection_table_missing"
# A walk past the exposure's page limit is bounded; a tenant needs more than
# this many pages of rows before the bound bites.
_MAX_PAGES = 100


@cache
def _metering_exposure() -> ProjectionTableConfig:
    """``metering-summary.v1`` exactly as its node contract declares it."""
    contract = yaml.safe_load(_METERING_CONTRACT.read_text(encoding="utf-8"))
    (exposure,) = load_projection_exposures_from_contract(
        contract, "node_projection_metering_summary", _METERING_CONTRACT
    )
    return exposure


def read_served_row(
    db_path: Path,
    tenant_id: str,
    window_kind: Literal["day", "all"],
    window_start: str,
    baseline_model: str,
) -> dict[str, Any] | None:
    """The tenant's row of ``metering-summary.v1``, read through the read node.

    The same read ``onex dashboard`` and the pages make (``HandlerProjectionRead``
    over the read-only local store), so what this prints is the served row.
    ``None`` when the store holds no such row.
    """
    exposure = _metering_exposure()
    handler = HandlerProjectionRead(
        topic_map={exposure.topic: exposure}, row_source=SqliteTableRowSource(db_path)
    )
    since: str | None = None
    for _ in range(_MAX_PAGES):
        result = asyncio.run(
            handler.handle(
                ModelProjectionReadRequest(
                    topic=exposure.topic, tenant_id=tenant_id, since=since
                )
            )
        )
        if not result.ok:
            if result.error == _TABLE_MISSING:
                return None
            raise MeteringRecordsUnavailableError(
                f"metering-summary.v1 read refused ({result.http_status} "
                f"{result.error}): {result.detail}"
            )
        for row in result.rows:
            if (row["window_kind"], row["window_start"], row["baseline_model"]) == (
                window_kind,
                window_start,
                baseline_model,
            ):
                return row
        if not result.truncated or result.next_cursor is None:
            return None
        since = result.next_cursor
    return None


def row_payload(row: dict[str, Any]) -> dict[str, Any]:
    """The served row as printed: its columns, with ``summary_json`` spread.

    Every key is one the served row carries, either as a column or inside
    ``summary_json``; where both name a key, the column wins. Nothing is
    derived here.
    """
    payload: dict[str, Any] = dict(row["summary_json"])
    payload.update({k: v for k, v in row.items() if k != "summary_json"})
    return payload


def _window_bounds(
    window_kind: Literal["day", "all"], window_start: str
) -> tuple[datetime | None, datetime | None]:
    if window_kind == "all":
        return None, None
    start = datetime.combine(date.fromisoformat(window_start), time.min, tzinfo=UTC)
    return start, start + timedelta(days=1)


def check_row_current(
    db_path: Path,
    row: dict[str, Any] | None,
    *,
    tenant_id: str,
    window_kind: Literal["day", "all"],
    window_start: str,
    baseline_model: str,
) -> ModelMeteringRowRefusal | None:
    """Why the served row cannot be printed, or None when it is current.

    Current means: no real run recorded in the row's window at or after its
    ``as_of``, and priced against the manifest version the baseline resolves to
    now. Reads only; never folds, writes or prices.
    """
    key = {
        "tenant_id": tenant_id,
        "window_kind": window_kind,
        "window_start": window_start,
        "baseline_model": baseline_model,
        "repair": METERING_ROW_REPAIR,
    }
    start, end = _window_bounds(window_kind, window_start)
    runs = read_metering_records(db_path=db_path, window_start=start, window_end=end)
    if row is None:
        if window_kind == "day" and not runs:
            reason = f"no delegation was recorded on {window_start} (UTC)"
        else:
            reason = (
                "no stored metering row for this window under baseline "
                f"{baseline_model}"
            )
        return ModelMeteringRowRefusal(
            state=EnumMeteringRowState.MISSING, reason=reason, **key
        )
    as_of = str(row["as_of"])
    stored_version = row["pricing_manifest_version"]
    later = [
        r.occurred_at for r in runs if r.occurred_at >= datetime.fromisoformat(as_of)
    ]
    if later:
        newest = max(later).astimezone(UTC).isoformat()
        return ModelMeteringRowRefusal(
            state=EnumMeteringRowState.STALE,
            reason=f"a delegation recorded at {newest} is not in this row (as of {as_of})",
            row_as_of=as_of,
            newest_run_at=newest,
            row_pricing_manifest_version=stored_version,
            **key,
        )
    version = current_pricing_manifest_version(baseline_model)
    if version != stored_version:
        return ModelMeteringRowRefusal(
            state=EnumMeteringRowState.STALE,
            reason=(
                f"priced against pricing manifest {stored_version}; the manifest "
                f"now resolves {version}"
            ),
            row_as_of=as_of,
            row_pricing_manifest_version=stored_version,
            current_pricing_manifest_version=version,
            **key,
        )
    return None


def _refusal(refusal: ModelMeteringRowRefusal, *, as_json: bool) -> NoReturn:
    if as_json:
        click.echo(json.dumps(refusal.model_dump(mode="json"), sort_keys=True))
    else:
        click.echo(
            f"{refusal.state.value}: {refusal.reason}\n"
            f"  tenant {refusal.tenant_id}, window {refusal.window_kind} "
            f"{refusal.window_start or '(all time)'}, baseline "
            f"{refusal.baseline_model}\n"
            f"  {refusal.repair}",
            err=True,
        )
    raise SystemExit(1)


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
    default=None,
    help=(
        "Read the row stored for this baseline model instead of the one the "
        "delegation resolves (resolve_baseline_model)."
    ),
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
@click.option("--json", "as_json", is_flag=True, help="Emit the stored row as JSON.")
@click.option(
    "--include-fixtures",
    is_flag=True,
    help=(
        "Refused: stored rows count real runs only, so a fixture-inclusive "
        "figure has no row to read."
    ),
)
def metering_command(
    window: str,
    day: datetime | None,
    baseline: str | None,
    db_path: Path | None,
    top: int,
    as_json: bool,
    include_fixtures: bool,
) -> None:
    """Read this install's own delegation metering and savings.

    Every figure is the stored metering row the end of each `onex delegate`
    refreshes. A run that recorded no measurement is counted as unknown, never
    as a zero, and a savings figure is never printed without the counterfactual
    baseline it was derived against.
    """
    if include_fixtures:
        # It used to refresh with the seed's fixture rows folded in and store
        # the result under the real row's key, where every reader took it for
        # measured figures (OMN-19970).
        raise click.UsageError(
            "--include-fixtures is refused: stored metering rows count real runs "
            "only, and this command reads stored rows without folding."
        )
    if day is not None and window.lower() != "all":
        raise click.UsageError("--day cannot be combined with --window today")
    resolved_db = db_path if db_path is not None else default_metering_db_path()
    selected_day: date | None = (
        day.date()
        if day
        else (datetime.now(tz=UTC).date() if window.lower() == "today" else None)
    )
    kind: Literal["day", "all"] = "day" if selected_day else "all"
    start = selected_day.isoformat() if selected_day else ""
    baseline_model = baseline or resolve_metering_baseline_model().model
    try:
        if not resolved_db.exists():
            raise MeteringRecordsUnavailableError(
                f"no local delegation evidence database at {resolved_db}"
            )
        # OMN-17427: the summary row is keyed on this install's own minted
        # tenant identity (`onex local init`), never a literal.
        tenant_id = str(require_local_tenant_identity(db_path=resolved_db))
        row = read_served_row(resolved_db, tenant_id, kind, start, baseline_model)
        refusal = check_row_current(
            resolved_db,
            row,
            tenant_id=tenant_id,
            window_kind=kind,
            window_start=start,
            baseline_model=baseline_model,
        )
    except MeteringRecordsUnavailableError as exc:
        raise click.ClickException(
            f"{exc}\nNo metering can be reported from an unreadable evidence store. "
            "Run `onex delegate` once to create it."
        ) from exc
    except LocalTenantIdentityError as exc:
        raise click.ClickException(str(exc)) from exc
    if refusal is not None:
        _refusal(refusal, as_json=as_json)
    assert row is not None  # check_row_current refuses an absent row
    if as_json:
        if row["baseline_state"] == EnumBaselineState.UNRESOLVED.value:
            click.echo(
                f"BASELINE_UNRESOLVED: {baseline_model} is not in the pricing "
                "manifest; savings_usd and counterfactual_usd are null, not 0.",
                err=True,
            )
        click.echo(json.dumps(row_payload(row), sort_keys=True))
        return
    summary = ModelMeteringSummary.model_validate(row["summary_json"])
    summary = summary.model_copy(update={"by_model": summary.by_model[:top]})
    click.echo(render_text(summary, resolved_db))
    click.echo(f"  row as of          {row['as_of']}")


if __name__ == "__main__":  # pragma: no cover - module entry for doubted installs
    # Runnable as `python -m omnimarket.cli.cli_metering` as well as through the
    # `onex metering` entry point. A console script only appears after the
    # package is reinstalled, and the host where you want this answer is often
    # precisely the host whose installed build you are doubting.
    metering_command()
