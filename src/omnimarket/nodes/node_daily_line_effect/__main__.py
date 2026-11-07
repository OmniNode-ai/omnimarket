# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run the daily line once (``daily``) or manage its 05:30 Eastern schedule (``schedule install|uninstall``)."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from omnimarket.nodes.node_daily_line_effect.handlers import schedule
from omnimarket.nodes.node_daily_line_effect.handlers.handler_daily_line_run import (
    build_runner,
    dump,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="node_daily_line_effect")
    verbs = parser.add_subparsers(dest="verb", required=True)
    verbs.add_parser("daily", help="compute, publish and write the STATUS row once")
    sched = verbs.add_parser("schedule", help="install or remove the daily timer")
    sched.add_argument("action", choices=("install", "uninstall"))
    sched.add_argument("--python", default=sys.executable)
    args = parser.parse_args(argv)

    if args.verb == "daily":
        publication = build_runner().run()
        sys.stdout.write(dump(publication) + "\n")
        return 0

    contract = schedule.load_schedule()
    home = Path.home()
    if args.action == "uninstall":
        return schedule.uninstall(contract, home=home)
    return schedule.install(
        contract,
        omni_root=Path(os.environ["OMNI_HOME"]),
        home=home,
        python=Path(args.python),
    )


if __name__ == "__main__":
    raise SystemExit(main())
