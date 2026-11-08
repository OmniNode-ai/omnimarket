# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_daily_line_effect <verb>``.

``daily``     compute, publish and write the STATUS row once.
``schedule``  ``install`` or ``uninstall`` the node's 05:30 Eastern timer.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from omnimarket.nodes.node_daily_line_effect.handlers import schedule
from omnimarket.nodes.node_daily_line_effect.handlers.handler_daily_line_run import (
    HandlerDailyLineRun,
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
        runner: HandlerDailyLineRun = build_runner()
        publication = runner.run()
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
