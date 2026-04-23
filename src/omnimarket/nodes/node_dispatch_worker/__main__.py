# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_dispatch_worker.

Compiles a worker dispatch spec into a role-templated agent prompt.

Usage:
    python -m omnimarket.nodes.node_dispatch_worker --name my-worker --team Omninode --role fixer --scope "Fix bug" --targets OMN-1234

Outputs JSON to stdout: ModelDispatchWorkerResult model.
"""

from __future__ import annotations

import argparse
import logging
import sys

from omnimarket.nodes.node_dispatch_worker.handlers.handler_dispatch_worker import (
    HandlerDispatchWorker,
)
from omnimarket.nodes.node_dispatch_worker.models.model_dispatch_worker_command import (
    EnumWorkerRole,
    ModelDispatchWorkerCommand,
)

_log = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(
        description="Compile a worker dispatch spec into a role-templated agent prompt."
    )
    parser.add_argument("--name", required=True, help="Worker handle")
    parser.add_argument("--team", required=True, help="Team name")
    parser.add_argument(
        "--role",
        required=True,
        choices=[r.value for r in EnumWorkerRole],
        help="Worker role",
    )
    parser.add_argument("--scope", required=True, help="Goal description")
    parser.add_argument(
        "--targets",
        nargs="+",
        required=True,
        help="Tickets/PRs/paths this worker owns",
    )
    parser.add_argument(
        "--collision-fences",
        nargs="*",
        default=[],
        help="Paths/repos this worker must not touch",
    )
    parser.add_argument(
        "--reports-to",
        default="team-lead",
        help="Agent to report to (default: team-lead)",
    )
    parser.add_argument(
        "--wall-clock-cap-min",
        type=int,
        default=None,
        help="Wall-clock cap in minutes [5, 480]",
    )
    parser.add_argument(
        "--model",
        default="sonnet",
        help="Model for Agent() spawn (default: sonnet)",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        default=False,
        help="Kill existing in_progress worker with same name and restart",
    )
    parser.add_argument(
        "--tasks-dir",
        type=str,
        default=None,
        help="Override TaskList directory (default: auto-resolved)",
    )
    parser.add_argument(
        "--json-input",
        type=str,
        default=None,
        help="Raw JSON ModelDispatchWorkerCommand (overrides individual flags)",
    )

    args = parser.parse_args()

    if args.json_input:
        command = ModelDispatchWorkerCommand.model_validate_json(args.json_input)
    else:
        command = ModelDispatchWorkerCommand(
            name=args.name,
            team=args.team,
            role=EnumWorkerRole(args.role),
            scope=args.scope,
            targets=args.targets,
            collision_fences=args.collision_fences,
            reports_to=args.reports_to,
            wall_clock_cap_min=args.wall_clock_cap_min,
            model=args.model,
            replace=args.replace,
        )

    handler = HandlerDispatchWorker()

    from pathlib import Path

    tasks_dir = Path(args.tasks_dir) if args.tasks_dir else None

    result = handler.handle(command, tasks_dir=tasks_dir)

    sys.stdout.write(result.model_dump_json(indent=2) + "\n")


if __name__ == "__main__":
    main()
