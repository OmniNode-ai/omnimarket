# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Record nightly rung evaluations or report missing cells."""

import argparse
import os
from datetime import UTC, date, datetime
from pathlib import Path

from omnimarket.delegation.rung_eval.cases import load_cases
from omnimarket.delegation.rung_eval.missing_nights import (
    detect_missing_nights,
    failing_gaps,
    format_report,
)
from omnimarket.delegation.rung_eval.rows_store import read_rows, write_night
from omnimarket.delegation.rung_eval.rungs import load_rungs
from omnimarket.delegation.rung_eval.runner import (
    PinnedDelegateTransport,
    RungTransport,
    run_night,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument(
        "--night", type=date.fromisoformat, default=datetime.now(UTC).date()
    )
    run.add_argument("--out-dir", type=Path, required=True)
    missing = commands.add_parser("check-missing")
    missing.add_argument("--dir", type=Path, required=True)
    missing.add_argument("--through", type=date.fromisoformat, required=True)
    missing.add_argument("--window-days", type=int, required=True)
    missing.add_argument(
        "--fail-window-days",
        type=int,
        default=1,
        help=(
            "only gaps on the newest N nights set exit 1; "
            "the report lists every gap in --window-days"
        ),
    )
    args = parser.parse_args()
    rungs, cases = load_rungs(), load_cases()
    if args.command == "run":
        transport = RungTransport(PinnedDelegateTransport(environ=os.environ))
        rows = run_night(args.night, rungs, cases, transport)
        write_night(args.out_dir, args.night, rows)
        for row in rows:
            print(row.model_dump_json())
        return 0
    if args.window_days < 1:
        parser.error("--window-days must be positive")
    if args.fail_window_days < 1:
        parser.error("--fail-window-days must be positive")
    gaps = detect_missing_nights(
        read_rows(args.dir), rungs, cases, args.through, args.window_days
    )
    if gaps:
        print(format_report(gaps))
        return 1 if failing_gaps(gaps, args.through, args.fail_window_days) else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
