# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_plans_today_effect``: the plans-today command line.

The flags are the script's own, and no flag selects the mode: the run resolves the caller through the
team identity declaration on every invocation, so the session-start hook's call
(``--linear-snapshot <linear-<UTC date>.txt> --orchestrator <lane>``) runs member mode in a member's
session with no new argument.
"""

from __future__ import annotations

import argparse
import os
import sys

from .handlers.handler_plans_today import (
    DEFAULT_SUMMARY_TIMEOUT_S,
    KB_ENV,
    SUMMARY_BUDGET_S,
    WATCHER_MAX_AGE_MIN,
    run_plans_today,
)
from .models.model_plans_today import ModelPlansTodayRequest


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog="plans_today",
        description="Bring up every relevant plan for the day, ranked, with the next action.",
    )
    ap.add_argument(
        "--json", action="store_true", help="print only the plans-today JSON on stdout"
    )
    ap.add_argument("--kb", help=f"the plans repository clone (default: {KB_ENV})")
    ap.add_argument(
        "--no-fetch",
        action="store_true",
        help="skip the git fetch of origin/main (tests only)",
    )
    ap.add_argument(
        "--watcher",
        help="pr-watcher state file (default: $OMNI_HOME/.claude_scratch/pr-watcher/state.json)",
    )
    ap.add_argument("--max-watcher-age-min", type=int, default=WATCHER_MAX_AGE_MIN)
    lg = ap.add_mutually_exclusive_group()
    lg.add_argument(
        "--linear-snapshot",
        help="the Linear snapshot the caller wrote; its .json sidecar beside it names the signed-in subject",
    )
    lg.add_argument(
        "--no-linear",
        action="store_true",
        help="run without Linear (dry runs and tests only)",
    )
    ap.add_argument(
        "--ledger-rows-file",
        help="read ledger rows from this file instead of the ledger reader (tests only)",
    )
    ap.add_argument("--window-days", type=int, default=14)
    ap.add_argument(
        "--plan",
        action="append",
        default=[],
        help="only plans whose path contains this text (repeatable)",
    )
    ap.add_argument(
        "--top",
        type=int,
        default=12,
        help="plans printed in the ranked list (the rest are counted)",
    )
    ap.add_argument("--all", action="store_true", help="print every selected plan")
    ap.add_argument("--summaries", choices=("top", "all", "none"), default="top")
    ap.add_argument("--summary-limit", type=int, default=8)
    ap.add_argument(
        "--summary-timeout",
        type=int,
        default=DEFAULT_SUMMARY_TIMEOUT_S,
        help="seconds one delegated call may take",
    )
    ap.add_argument(
        "--summaries-budget",
        type=int,
        default=None,
        help=f"seconds for the whole delegation phase (default {SUMMARY_BUDGET_S})",
    )
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument(
        "--out-dir",
        help="output directory (default: $OMNI_HOME/.claude_scratch/plans-today/<UTC date>)",
    )
    ap.add_argument(
        "--no-msg",
        action="store_true",
        help="dry run: emit no ledger MSG (never used from session start)",
    )
    ap.add_argument(
        "--orchestrator",
        default=os.environ.get("SESSION_LANE", "orchestrator"),
        help="lane the MSG is addressed to (default: SESSION_LANE, else orchestrator)",
    )
    ap.add_argument(
        "--lane", default="plans-today", help="the from= lane of the MSG rows"
    )
    ap.add_argument("--now", help="override the clock, UTC ISO (tests only)")
    return ap.parse_args(argv)


def request_from_args(a: argparse.Namespace) -> ModelPlansTodayRequest:
    return ModelPlansTodayRequest(
        json_only=a.json,
        kb=a.kb,
        no_fetch=a.no_fetch,
        watcher=a.watcher,
        max_watcher_age_min=a.max_watcher_age_min,
        linear_snapshot=a.linear_snapshot,
        no_linear=a.no_linear,
        ledger_rows_file=a.ledger_rows_file,
        window_days=a.window_days,
        plan=tuple(a.plan),
        top=a.top,
        all=a.all,
        summaries=a.summaries,
        summary_limit=a.summary_limit,
        summary_timeout=a.summary_timeout,
        summaries_budget=a.summaries_budget,
        workers=a.workers,
        out_dir=a.out_dir,
        no_msg=a.no_msg,
        orchestrator=a.orchestrator,
        lane=a.lane,
        now=a.now,
    )


def main(argv: list[str] | None = None) -> int:
    result = run_plans_today(request_from_args(parse_args(argv)))
    if result.refused is not None:
        sys.stderr.write(f"REFUSED plans-today: {result.refused}\n")
    elif result.stdout:
        sys.stdout.write(result.stdout + "\n")
    return result.exit_code
