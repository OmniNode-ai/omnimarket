# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read work record rows from stdin, run the handler and print the day-over-day report (OMN-26212)."""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, timedelta

from omnimarket.models.model_day_over_day_report import (
    ModelDayOverDayReport,
    ModelDayOverDayRequest,
    ModelFigure,
)
from omnimarket.nodes.node_day_over_day_report_compute.handlers.handler_day_over_day_report import (
    HandlerDayOverDayReport,
)
from omnimarket.nodes.node_day_over_day_report_compute.handlers.ledger_readings import (
    read_day,
)


def _ledger_text() -> str | None:
    """The work record rows arrive on stdin; an empty or absent stream is a source that was not read."""
    if sys.stdin is None or sys.stdin.isatty():
        return None
    text = sys.stdin.read()
    return text if text.strip() else None


def _fig(f: ModelFigure) -> str:
    return f"{f.value} [{f.source}{' @ ' + f.read_at.strftime('%H:%MZ') if f.read_at else ''}]"


def render(r: ModelDayOverDayReport) -> str:
    """The report as plain lines; every UNREADABLE part is printed, never dropped."""
    out = [
        f"DAY-OVER-DAY {r.yesterday_label} -> {r.today_label} as_of={r.as_of:%Y-%m-%dT%H:%M:%SZ}"
    ]
    out.append(
        f"newly running ({r.newly_running.reading}): {len(r.newly_running.entries)}"
    )
    out += [f"  + {e.change} on {e.host} [{e.source}]" for e in r.newly_running.entries]
    out.append(
        f"board changes ({r.board_changes.reading}): {len(r.board_changes.entries)}"
    )
    out += [
        f"  {e.check_id}: {e.before} -> {e.after} [{e.source}]"
        for e in r.board_changes.entries
    ]
    t = r.throughput
    out.append(
        f"throughput: merges {_fig(t.merges_yesterday)} -> {_fig(t.merges_today)}"
    )
    out.append(
        f"throughput: seen running {_fig(t.running_yesterday)} -> {_fig(t.running_today)}"
    )
    out.append(
        f"cost trend ({r.cost_trend.reading}): engines {len(r.cost_trend.entries)}"
    )
    out += [
        f"  {e.engine}: calls {e.calls_yesterday.value} -> {e.calls_today.value}; tokens {e.tokens_today.value}; dollars {e.dollars_today.value}"
        for e in r.cost_trend.entries
    ]
    out.append(f"regressions ({r.regressions.reading}): {len(r.regressions.entries)}")
    out += [
        f"  ! {e.kind} {e.subject}: {e.detail} [{e.source}]"
        for e in r.regressions.entries
    ]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--today", help="UTC date YYYY-MM-DD (default: today)")
    parser.add_argument(
        "--yesterday", help="UTC date (default: the day before --today)"
    )
    parser.add_argument(
        "--merges-today", type=int, help="from node_merged_running_count_compute"
    )
    parser.add_argument(
        "--merges-yesterday", type=int, help="from node_merged_running_count_compute"
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    now = datetime.now(UTC)
    today = args.today or f"{now:%Y-%m-%d}"
    yesterday = (
        args.yesterday
        or f"{datetime.strptime(today, '%Y-%m-%d') - timedelta(days=1):%Y-%m-%d}"
    )
    text = _ledger_text()
    src = "node_merged_running_count_compute (supplied)"
    y = read_day(text, yesterday).model_copy(
        update={
            "merges": args.merges_yesterday,
            "merges_source": src if args.merges_yesterday is not None else "",
        }
    )
    t = read_day(text, today).model_copy(
        update={
            "merges": args.merges_today,
            "merges_source": src if args.merges_today is not None else "",
        }
    )
    report = HandlerDayOverDayReport().handle(
        ModelDayOverDayRequest(yesterday=y, today=t, as_of=now)
    )
    sys.stdout.write(
        (report.model_dump_json(indent=2) if args.json else render(report)) + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
