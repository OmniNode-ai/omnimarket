# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_delegation_readiness_verdict_effect <verb>``.

``run``       read the scenarios' results, publish the verdict and write the STATUS row once; exit 0 only on PASS,
              1 on FAIL (each scenario that misses is named on stdout).
``schedule``  ``install`` or ``uninstall`` the node's hourly timer.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from omnimarket.handlers import delegation_scenario_runtime as runtime_parts
from omnimarket.nodes.node_delegation_readiness_verdict_effect.handlers.handler_readiness_verdict_run import (
    NODE_MODULE,
    HandlerReadinessVerdictRun,
    build_runner,
    load_schedule,
    report_lines,
)


def main(
    argv: Sequence[str] | None = None,
    *,
    runner_factory: Callable[[], HandlerReadinessVerdictRun] = build_runner,
) -> int:
    parser = argparse.ArgumentParser(prog="node_delegation_readiness_verdict_effect")
    verbs = parser.add_subparsers(dest="verb", required=True)
    verbs.add_parser("run", help="read, compute, publish and write the STATUS row once")
    sched = verbs.add_parser("schedule", help="install or remove the hourly timer")
    sched.add_argument("action", choices=("install", "uninstall"))
    sched.add_argument("--python", default=sys.executable)
    args = parser.parse_args(argv)

    if args.verb == "run":
        verdict = runner_factory().run()
        sys.stdout.write("\n".join(report_lines(verdict)) + "\n")
        return 0 if verdict.passed else 1

    schedule = load_schedule()
    home = Path.home()
    if args.action == "uninstall":
        return runtime_parts.uninstall_schedule(schedule, home=home)
    return runtime_parts.install_schedule(
        schedule,
        node_module=NODE_MODULE,
        omni_root=Path(os.environ["OMNI_HOME"]),  # ONEX_FLAG_EXEMPT
        home=home,
        python=Path(args.python),
    )
