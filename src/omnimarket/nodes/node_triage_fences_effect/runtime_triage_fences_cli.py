# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run one fence derivation in process and print the report as JSON (OMN-24248, moved from OMN-20682).

The report is the JSON the lab fill and the triage lanes read. The ledger path is required and has
no default; the live window, the pull-request window, the repositories and the receipt folder come
from the node overlay (``<root>/node_triage_fences_effect/overlay.yaml`` under
``ONEX_SKILL_OVERLAY_ROOTS``), and a missing overlay is a usage error that says so.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from omnimarket.nodes.node_triage_fences_compute.handlers.handler_triage_fences_grammar import (
    parse_ts,
)
from omnimarket.nodes.node_triage_fences_effect.handlers.handler_triage_fences import (
    HandlerTriageFences,
)
from omnimarket.nodes.node_triage_fences_effect.models import (
    ModelTriageFencesOptions,
    ModelTriageFencesRequest,
)
from omnimarket.nodes.node_triage_fences_effect.overlay import (
    load_triage_fences_overlay,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="onex-triage-fences-node",
        description=(
            "Derive the triage fence list mechanically from the rolling work ledger. The fence "
            "list is never hand-derived (OMN-17757)."
        ),
    )
    # Required positional, deliberately with NO default: an omitted ledger is a usage error.
    parser.add_argument(
        "ledger", help="path to the rolling work ledger markdown file (required)"
    )
    parser.add_argument(
        "--open-prs",
        action="store_true",
        help="also fence tickets with an open PR in the registry (reads the PR watcher's state)",
    )
    parser.add_argument(
        "--pr-window-hours",
        type=float,
        default=None,
        help="open-PR recency window; default comes from the node overlay",
    )
    parser.add_argument(
        "--repos",
        default=None,
        help="comma-separated repos to sweep with --open-prs; default comes from the node overlay",
    )
    parser.add_argument(
        "--now",
        default=None,
        help="UTC instant to age claims against (ISO 8601); default: now",
    )
    parser.add_argument(
        "--stale-after-hours",
        type=float,
        default=None,
        help=(
            "a CLAIM fences only while its lane is live: its runner receipt reads running or it "
            "wrote a row within this many hours; default comes from the node overlay; the "
            "others are reported as stale_claims"
        ),
    )
    parser.add_argument("--indent", type=int, default=2, help="JSON indent")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    ledger = Path(args.ledger)
    if not ledger.is_file():
        # Fail fast and loud: a bad path must never render as "nothing is fenced".
        parser.error(f"ledger path is not a readable file: {ledger}")
    try:
        overlay = load_triage_fences_overlay()
    except Exception as exc:
        parser.error(f"node overlay (ONEX_SKILL_OVERLAY_ROOTS): {exc}")
    if args.stale_after_hours is not None and not args.stale_after_hours > 0:
        parser.error(
            f"--stale-after-hours must be positive, got {args.stale_after_hours}"
        )
    replay: datetime | None = None
    if args.now:
        replay = parse_ts(args.now)
        if replay is None:
            parser.error(f"--now is not an ISO 8601 UTC instant: {args.now}")

    request = ModelTriageFencesRequest(
        include_open_prs=args.open_prs,
        repos=(
            tuple(r.strip() for r in args.repos.split(",") if r.strip())
            if args.repos is not None
            else None
        ),
        pr_window_hours=args.pr_window_hours,
        stale_after_hours=args.stale_after_hours,
        now=replay or datetime.now(UTC),
    )
    result = HandlerTriageFences(
        ModelTriageFencesOptions(ledger=ledger), overlay
    ).handle(request)
    if result.report is None:
        print(result.error, file=sys.stderr)
        return result.exit_code
    json.dump(result.report.model_dump(mode="json"), sys.stdout, indent=args.indent)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
