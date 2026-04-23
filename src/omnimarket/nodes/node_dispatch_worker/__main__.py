# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_dispatch_worker.

Compiles a worker dispatch spec into a role-templated agent prompt.
This is a PURE PREP node — no Agent() calls, no external API calls.

Usage:
    # Convenience shortcut used by the overseer anti-passivity tick:
    python -m omnimarket.nodes.node_dispatch_worker --ticket OMN-9438

    # Full form:
    python -m omnimarket.nodes.node_dispatch_worker \\
        --name my-fixer \\
        --team Omninode \\
        --role fixer \\
        --scope "Fix the dispatch worker CLI gap" \\
        --targets OMN-9438

    # Dry-run (prints compiled spec without running handler):
    python -m omnimarket.nodes.node_dispatch_worker --ticket OMN-9438 --dry-run

    # Raw JSON output:
    python -m omnimarket.nodes.node_dispatch_worker --ticket OMN-9438 --json

Outputs:
    Human-readable summary (default) or raw JSON (--json flag).
    Exit 0 on success, exit 1 on validation error or handler exception.

Related:
    - OMN-9438: add __main__.py CLI to node_dispatch_worker
    - Overseer anti-passivity tick: invokes via --ticket <id>
"""

from __future__ import annotations

import argparse
import json
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

_VALID_ROLES = [r.value for r in EnumWorkerRole]


def _name_from_ticket(ticket_id: str) -> str:
    """Derive a worker name from a ticket ID.

    e.g. "OMN-9438" -> "omn-9438-fixer"
    """
    return ticket_id.lower().replace("_", "-") + "-fixer"


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(
        prog="python -m omnimarket.nodes.node_dispatch_worker",
        description=(
            "Compile a worker dispatch spec into a role-templated agent prompt. "
            "Pure prep node — no Agent() calls, no external API calls."
        ),
    )

    # --- convenience shortcut ---
    parser.add_argument(
        "--ticket",
        metavar="OMN-XXXX",
        default=None,
        help=(
            "Convenience shortcut: sets --targets OMN-XXXX, --role fixer, "
            "--name <ticket>-fixer, --team Omninode. "
            "Override any field explicitly to override the shortcut default."
        ),
    )

    # --- full form args ---
    parser.add_argument(
        "--name",
        metavar="WORKER_HANDLE",
        default=None,
        help="Worker handle (lowercase, hyphens/underscores ok, max 64 chars)",
    )
    parser.add_argument(
        "--team",
        metavar="TEAM",
        default=None,
        help="Team name for task scoping (default: Omninode when --ticket is used)",
    )
    parser.add_argument(
        "--role",
        metavar="ROLE",
        default=None,
        choices=_VALID_ROLES,
        help=f"Worker role. One of: {', '.join(_VALID_ROLES)} (default: fixer when --ticket is used)",
    )
    parser.add_argument(
        "--scope",
        metavar="DESCRIPTION",
        default=None,
        help="Goal description for the worker",
    )
    parser.add_argument(
        "--targets",
        metavar="TARGET",
        action="append",
        default=None,
        help="Tickets/PRs/paths this worker owns (repeatable, e.g. --targets OMN-9438 --targets omnimarket#123)",
    )
    parser.add_argument(
        "--collision-fences",
        metavar="FENCE",
        action="append",
        default=None,
        dest="collision_fences",
        help="Manual collision fences (repeatable, optional — auto-populated if omitted)",
    )
    parser.add_argument(
        "--reports-to",
        metavar="AGENT",
        default="team-lead",
        dest="reports_to",
        help="Agent to report to (default: team-lead)",
    )
    parser.add_argument(
        "--wall-clock-cap-min",
        metavar="MINUTES",
        type=int,
        default=None,
        dest="wall_clock_cap_min",
        help="Wall-clock cap in minutes [5, 480] (default: role-specific)",
    )
    parser.add_argument(
        "--model",
        metavar="MODEL",
        default="sonnet",
        help="Model for Agent() spawn (default: sonnet)",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        default=False,
        help="Kill existing in_progress worker with same name and restart",
    )

    # --- output control ---
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        dest="dry_run",
        help="Print what would be compiled without running the handler",
    )
    parser.add_argument(
        "--json",
        dest="output_json",
        action="store_true",
        default=False,
        help="Output raw JSON (default: human-readable summary)",
    )

    args = parser.parse_args()

    # --- apply --ticket shortcut defaults ---
    if args.ticket is not None:
        if args.targets is None:
            # No explicit --targets: ticket ID is the sole target
            args.targets = [args.ticket]
        elif args.ticket not in args.targets:
            # Explicit --targets given: prepend the ticket so the handler
            # can derive both ticket and repo from the combined target list
            args.targets = [args.ticket, *args.targets]
        if args.name is None:
            args.name = _name_from_ticket(args.ticket)
        if args.team is None:
            args.team = "Omninode"
        if args.role is None:
            args.role = EnumWorkerRole.fixer.value
        if args.scope is None:
            args.scope = f"Implement ticket {args.ticket}"

    # --- validate required fields are present ---
    missing: list[str] = []
    for field, label in [
        (args.name, "--name"),
        (args.team, "--team"),
        (args.role, "--role"),
        (args.scope, "--scope"),
        (args.targets, "--targets"),
    ]:
        if not field:
            missing.append(label)

    if missing:
        parser.error(
            f"The following arguments are required (or use --ticket for defaults): "
            f"{', '.join(missing)}"
        )

    # --- dry-run: print compiled spec without running handler ---
    if args.dry_run:
        dry_payload: dict[str, object] = {
            "dry_run": True,
            "name": args.name,
            "team": args.team,
            "role": args.role,
            "scope": args.scope,
            "targets": args.targets,
            "collision_fences": args.collision_fences or [],
            "reports_to": args.reports_to,
            "wall_clock_cap_min": args.wall_clock_cap_min,
            "model": args.model,
            "replace": args.replace,
        }
        sys.stdout.write(json.dumps(dry_payload, indent=2) + "\n")
        sys.exit(0)

    # --- build and validate the command model ---
    try:
        command = ModelDispatchWorkerCommand(
            name=args.name,
            team=args.team,
            role=EnumWorkerRole(args.role),
            scope=args.scope,
            targets=args.targets,
            collision_fences=args.collision_fences or [],
            reports_to=args.reports_to,
            wall_clock_cap_min=args.wall_clock_cap_min,
            model=args.model,
            replace=args.replace,
        )
    except Exception as exc:
        sys.stderr.write(f"Validation error: {exc}\n")
        sys.exit(1)

    # --- run handler ---
    try:
        handler = HandlerDispatchWorker()
        result = handler.handle(command)
    except Exception as exc:
        sys.stderr.write(f"Handler error: {exc}\n")
        sys.exit(1)

    # --- output ---
    if args.output_json:
        sys.stdout.write(result.model_dump_json(indent=2) + "\n")
    else:
        _render_human(result)

    # exit 1 if rejected
    if result.rejected_reason:
        sys.exit(1)

    sys.exit(0)


def _render_human(result: object) -> None:
    """Print a human-readable dispatch compilation report."""
    from omnimarket.nodes.node_dispatch_worker.models.model_dispatch_worker_result import (
        ModelDispatchWorkerResult,
    )

    if not isinstance(result, ModelDispatchWorkerResult):
        sys.stdout.write(str(result) + "\n")
        return

    spawn = result.proposed_agent_spawn_args
    name = spawn.get("name", "unknown")
    team = spawn.get("team_name", "unknown")
    model = spawn.get("model", "unknown")

    sys.stdout.write(f"\nDispatch Worker Compilation — {name}\n")
    sys.stdout.write("=" * 50 + "\n\n")

    if result.rejected_reason:
        sys.stdout.write("Status: REJECTED\n")
        sys.stdout.write(f"Reason: {result.rejected_reason}\n\n")
        return

    sys.stdout.write("Status: OK\n")
    sys.stdout.write(f"Name:   {name}\n")
    sys.stdout.write(f"Team:   {team}\n")
    sys.stdout.write(f"Model:  {model}\n")

    if result.validated_task_description:
        sys.stdout.write(f"Task:   {result.validated_task_description}\n")

    if result.collision_fence_embeds:
        sys.stdout.write(
            f"Fences: {len(result.collision_fence_embeds)} active collision fence(s)\n"
        )
    else:
        sys.stdout.write("Fences: none\n")

    if result.validated_task_description:
        desc = result.validated_task_description[:200]
        if len(result.validated_task_description) > 200:
            desc += "..."
        sys.stdout.write(f"\nTask description (truncated):\n  {desc}\n")

    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
