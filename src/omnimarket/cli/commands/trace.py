# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI trace subcommand group — event bus observability for ONEX.

Queries the projection API to render structured log entries with ANSI color.

Base URL is read from OMNIDASH_API_URL (default: http://localhost:3002).
The projection API topic for log entries is the log_projection snapshot topic;
CLI uses the generic /projection/{topic} endpoint.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from typing import Any

import click
import httpx

from omnimarket.cli.market import market

_DEFAULT_BASE_URL = "http://localhost:3002"
_LOG_ENTRIES_TOPIC = "onex.evt.platform.log-entry.v1"  # onex-topic-allow: CLI constant, not a handler literal

# ---------------------------------------------------------------------------
# ANSI helpers
# ---------------------------------------------------------------------------

_LEVEL_COLORS: dict[str, dict[str, Any]] = {
    "debug": {"dim": True},
    "info": {},
    "warning": {"fg": "yellow"},
    "error": {"fg": "red"},
    "critical": {"fg": "red", "bold": True},
}

_STATUS_COLORS: dict[str, dict[str, Any]] = {
    "running": {"fg": "green"},
    "done": {"dim": True},
    "error": {"fg": "red"},
}


def _style_level(level: str) -> str:
    style = _LEVEL_COLORS.get(level.lower(), {})
    return click.style(level.upper().ljust(8), **style)


def _style_node(name: str) -> str:
    return click.style(name, fg="cyan")


def _style_ts(ts: str) -> str:
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        short = dt.strftime("%H:%M:%S.") + f"{dt.microsecond // 1000:03d}"
    except (ValueError, AttributeError):
        short = ts
    return click.style(short, dim=True)


def _style_status(status: str) -> str:
    style = _STATUS_COLORS.get(status.lower(), {})
    symbol = (
        "●"
        if status.lower() == "running"
        else ("✓" if status.lower() == "done" else "✗")
    )
    return click.style(f"{symbol} {status}", **style)


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------


def _base_url() -> str:
    return os.environ.get("OMNIDASH_API_URL", _DEFAULT_BASE_URL).rstrip("/")


def _fetch_projection(
    client: httpx.Client,
    *,
    topic: str,
    correlation_id: str | None = None,
) -> dict[str, Any]:
    """Fetch rows from the generic projection endpoint."""
    url = f"{_base_url()}/projection/{topic}"
    params: dict[str, str] = {}
    if correlation_id:
        params["correlation_id"] = correlation_id
    try:
        resp = client.get(url, params=params, timeout=10.0)
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]
    except httpx.HTTPStatusError as exc:
        raise click.ClickException(
            f"Projection API error {exc.response.status_code}: {exc.response.text}"
        ) from exc
    except httpx.RequestError as exc:
        raise click.ClickException(
            f"Could not reach projection API at {_base_url()}: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Trace group
# ---------------------------------------------------------------------------


@market.group("trace")
def trace() -> None:
    """Query and watch ONEX event bus traces."""


# ---------------------------------------------------------------------------
# trace list
# ---------------------------------------------------------------------------


@trace.command("list")
@click.option("--since", default=None, help="ISO timestamp — only traces after this.")
@click.option("--limit", default=50, show_default=True, help="Max traces to show.")
@click.option("--running-only", is_flag=True, help="Show only in-progress traces.")
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["text", "json"]),
    default="text",
    show_default=True,
    help="Output format.",
)
def trace_list(
    since: str | None,
    limit: int,
    running_only: bool,
    output_format: str,
) -> None:
    """List recent traces grouped by correlation_id."""
    with httpx.Client() as client:
        payload = _fetch_projection(client, topic=_LOG_ENTRIES_TOPIC)

    rows: list[dict[str, Any]] = payload.get("rows", [])

    if since:
        rows = [r for r in rows if r.get("timestamp", "") >= since]

    # Group by correlation_id
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        cid = row.get("correlation_id") or "<none>"
        groups.setdefault(cid, []).append(row)

    # Build trace summaries
    summaries: list[dict[str, Any]] = []
    for cid, entries in groups.items():
        nodes = sorted({e.get("node_name", "") for e in entries})
        latest = max(entries, key=lambda e: e.get("timestamp", ""))
        has_error = any(
            e.get("level", "").lower() in {"error", "critical"} for e in entries
        )
        # Determine status: error > done
        status = "error" if has_error else "done"

        first_ts = min(entries, key=lambda e: e.get("timestamp", ""))["timestamp"]
        last_ts = latest["timestamp"]
        try:
            t0 = datetime.fromisoformat(first_ts.replace("Z", "+00:00"))
            t1 = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
            duration_s = (t1 - t0).total_seconds()
            duration_str = f"{duration_s:.1f}s"
        except (ValueError, TypeError):
            duration_str = "?"

        summaries.append(
            {
                "correlation_id": cid,
                "nodes": nodes,
                "node_count": len(nodes),
                "event_count": len(entries),
                "duration": duration_str,
                "status": status,
                "latest_message": latest.get("message", ""),
            }
        )

    if running_only:
        summaries = [s for s in summaries if s["status"] == "running"]

    summaries = summaries[:limit]

    if output_format == "json":
        click.echo(json.dumps(summaries, indent=2))
        return

    if not summaries:
        click.echo("No traces found.")
        return

    header = click.style("CORRELATION ID         ", bold=True) + click.style(
        "NODES  EVENTS  DURATION   STATUS         LATEST MESSAGE", bold=True
    )
    click.echo(header)
    click.echo("-" * 90)

    for s in summaries:
        cid_short = s["correlation_id"][:20].ljust(22)
        nodes_col = str(s["node_count"]).ljust(7)
        events_col = str(s["event_count"]).ljust(7)
        dur_col = s["duration"].ljust(11)
        status_col = _style_status(s["status"]).ljust(24)
        msg = s["latest_message"][:40]
        click.echo(f"{cid_short}{nodes_col}{events_col}{dur_col}{status_col}{msg}")


# ---------------------------------------------------------------------------
# trace query
# ---------------------------------------------------------------------------


@trace.command("query")
@click.option("--correlation-id", required=True, help="Trace correlation ID to query.")
@click.option("--since", default=None, help="ISO timestamp — only events after this.")
@click.option("--limit", default=200, show_default=True, help="Max events to show.")
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["text", "json", "yaml"]),
    default="text",
    show_default=True,
    help="Output format.",
)
def trace_query(
    correlation_id: str,
    since: str | None,
    limit: int,
    output_format: str,
) -> None:
    """Query all events for a specific trace correlation ID."""
    with httpx.Client() as client:
        payload = _fetch_projection(
            client, topic=_LOG_ENTRIES_TOPIC, correlation_id=correlation_id
        )

    rows: list[dict[str, Any]] = payload.get("rows", [])

    if since:
        rows = [r for r in rows if r.get("timestamp", "") >= since]

    rows = sorted(rows, key=lambda r: r.get("timestamp", ""))
    rows = rows[:limit]

    if output_format == "json":
        click.echo(json.dumps(rows, indent=2))
        return

    if output_format == "yaml":
        import yaml

        click.echo(yaml.safe_dump(rows, sort_keys=False))
        return

    if not rows:
        click.echo(f"No events found for correlation_id={correlation_id!r}.")
        return

    for row in rows:
        ts = _style_ts(row.get("timestamp", ""))
        node = _style_node(row.get("node_name", "").ljust(24))
        level = _style_level(row.get("level", "info"))
        msg = row.get("message", "")
        click.echo(f"{ts}  {node}  {level}  {msg}")


# ---------------------------------------------------------------------------
# trace watch
# ---------------------------------------------------------------------------


@trace.command("watch")
@click.option("--correlation-id", default=None, help="Filter by correlation ID.")
@click.option("--node", default=None, help="Filter by node name.")
@click.option(
    "--level",
    default=None,
    type=click.Choice(["debug", "info", "warning", "error", "critical"]),
    help="Filter by log level.",
)
@click.option(
    "--interval",
    default=2.0,
    show_default=True,
    type=float,
    help="Poll interval in seconds.",
)
def trace_watch(
    correlation_id: str | None,
    node: str | None,
    level: str | None,
    interval: float,
) -> None:
    """Watch live events — polls projection API and shows new entries."""
    seen_ids: set[str] = set()

    filters = []
    if correlation_id:
        filters.append(f"correlation_id={correlation_id}")
    if node:
        filters.append(f"node={node}")
    if level:
        filters.append(f"level={level}")
    filter_desc = ", ".join(filters) if filters else "all traces"
    click.echo(
        click.style(
            f"Watching {filter_desc} (interval={interval}s) — Ctrl-C to exit", dim=True
        )
    )

    try:
        while True:
            with httpx.Client() as client:
                try:
                    payload = _fetch_projection(
                        client,
                        topic=_LOG_ENTRIES_TOPIC,
                        correlation_id=correlation_id,
                    )
                except click.ClickException as exc:
                    click.echo(
                        click.style(f"[poll error] {exc.format_message()}", fg="red"),
                        err=True,
                    )
                    time.sleep(interval)
                    continue

            rows: list[dict[str, Any]] = payload.get("rows", [])

            # Apply filters
            if node:
                rows = [r for r in rows if r.get("node_name") == node]
            if level:
                rows = [r for r in rows if r.get("level", "").lower() == level.lower()]

            # Show only new rows
            new_rows = [r for r in rows if r.get("entry_id") not in seen_ids]
            new_rows = sorted(new_rows, key=lambda r: r.get("timestamp", ""))

            for row in new_rows:
                entry_id = row.get("entry_id", "")
                seen_ids.add(entry_id)
                ts = _style_ts(row.get("timestamp", ""))
                nd = _style_node(row.get("node_name", "").ljust(24))
                lv = _style_level(row.get("level", "info"))
                msg = row.get("message", "")
                click.echo(f"{ts}  {nd}  {lv}  {msg}")

            time.sleep(interval)
    except KeyboardInterrupt:
        click.echo(click.style("\nWatch stopped.", dim=True))


__all__: list[str] = ["trace"]
