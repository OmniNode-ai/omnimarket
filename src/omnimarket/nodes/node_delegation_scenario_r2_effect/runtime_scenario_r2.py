# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_delegation_scenario_r2_effect <verb>``.

``run``       run scenario R2 once, write its STATUS row and emit its result on the bus (``--manual`` marks the run manual,
              which never fills a schedule slot).
``schedule``  ``install`` or ``uninstall`` the node's hourly timer.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from omnimarket.handlers import delegation_scenario_runtime as runtime_parts
from omnimarket.models.model_delegation_scenario import EnumScenarioTrigger
from omnimarket.nodes.node_delegation_scenario_r2_effect.handlers.handler_scenario_r2 import (
    NODE_MODULE,
    build_runner,
    load_schedule,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="node_delegation_scenario_r2_effect")
    verbs = parser.add_subparsers(dest="verb", required=True)
    run = verbs.add_parser("run", help="run scenario R2 once")
    run.add_argument("--manual", action="store_true", help="mark the run manual")
    sched = verbs.add_parser("schedule", help="install or remove the hourly timer")
    sched.add_argument("action", choices=("install", "uninstall"))
    sched.add_argument("--python", default=sys.executable)
    args = parser.parse_args(argv)

    if args.verb == "run":
        trigger = (
            EnumScenarioTrigger.MANUAL if args.manual else EnumScenarioTrigger.SCHEDULE
        )
        result = build_runner(trigger=trigger).run()
        sys.stdout.write(result.model_dump_json(indent=2) + "\n")
        return 0 if result.conclusion.value == "success" else 1

    schedule = load_schedule()
    home = Path.home()
    if args.action == "uninstall":
        return runtime_parts.uninstall_schedule(schedule, home=home)
    return runtime_parts.install_schedule(
        schedule,
        node_module=NODE_MODULE,
        omni_root=Path(os.environ["OMNI_HOME"]),
        home=home,
        python=Path(args.python),
    )
