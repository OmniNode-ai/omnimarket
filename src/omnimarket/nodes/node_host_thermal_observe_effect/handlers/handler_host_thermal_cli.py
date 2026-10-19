# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""onex-host-thermal: record one reading (the timer's command) or print the series report."""

from __future__ import annotations

import argparse
import os
import socket
import sys
from collections.abc import Sequence
from pathlib import Path

from ..models import ModelHostThermalObserveRequest
from .handler_host_thermal_observe import HandlerHostThermalObserve
from .handler_host_thermal_report import report


def _state_dir(arg: Path | None) -> Path:
    if arg is not None:
        return arg.expanduser()
    return Path(os.environ["ONEX_HOST_THERMAL_STATE_DIR"]).expanduser()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="onex-host-thermal")
    commands = parser.add_subparsers(dest="command", required=True)
    observe = commands.add_parser(
        "observe", help="append one reading to the host's series"
    )
    observe.add_argument("--state-dir", type=Path)
    observe.add_argument("--host", default=socket.gethostname())
    observe.add_argument("--sample-seconds", type=float, default=1.0)
    observe.add_argument("--dry-run", action="store_true")
    summary = commands.add_parser(
        "report", help="temperature by load bucket and by hour"
    )
    summary.add_argument("--state-dir", type=Path)
    summary.add_argument(
        "--since", help="ISO UTC lower bound, e.g. 2026-10-09T16:00:00Z"
    )
    summary.add_argument("--cores", type=int, default=os.cpu_count() or 1)
    args = parser.parse_args(argv)
    try:
        state_dir = _state_dir(args.state_dir)
    except KeyError:
        print(
            "onex-host-thermal: pass --state-dir or set ONEX_HOST_THERMAL_STATE_DIR",
            file=sys.stderr,
        )
        return 2
    if args.command == "report":
        print(report(state_dir, args.cores, args.since))
        return 0
    reading = HandlerHostThermalObserve().handle(
        ModelHostThermalObserveRequest(
            host=args.host,
            state_dir=state_dir,
            sample_seconds=args.sample_seconds,
            dry_run=args.dry_run,
        )
    )
    print(reading.line())
    for error in reading.errors:
        print(error, file=sys.stderr)
    return int(reading.cpu_temp_c is None and reading.load1 is None)


if __name__ == "__main__":
    raise SystemExit(main())
