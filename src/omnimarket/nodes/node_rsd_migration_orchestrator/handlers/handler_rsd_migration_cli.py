# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_rsd_migration_orchestrator``: check-shape and run."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from omnimarket.models.model_delegated_code_edit import (
    ModelCodeEditResult,
    ModelDelegatedCodeEditRequest,
)
from omnimarket.nodes.node_rsd_migration_orchestrator.handlers.handler_rsd_migration_orchestrator import (
    HandlerRsdMigrationOrchestrator,
    ProtocolCodeEditRunner,
    canonical_shape_problems,
)
from omnimarket.nodes.node_rsd_migration_orchestrator.models.model_rsd_migration import (
    ModelRsdMigrationTask,
)


def _orchestrator_runner(
    *, state_root: Path, onex: Path, delegate_flags: tuple[str, ...]
) -> ProtocolCodeEditRunner:
    from omnimarket.delegated_code_edit.loop_ports import DelegatedCodeEditPorts
    from omnimarket.nodes.node_delegated_code_edit_orchestrator import (
        HandlerDelegatedCodeEditOrchestrator,
    )

    ports = DelegatedCodeEditPorts(
        onex=onex, state_root=state_root, delegate_flags=delegate_flags
    )

    def run(request: ModelDelegatedCodeEditRequest) -> ModelCodeEditResult:
        return HandlerDelegatedCodeEditOrchestrator(ports).run(request)

    return run


def main(argv: list[str] | None = None) -> int:
    """``check-shape`` checks one node; ``run`` runs one migration."""
    parser = argparse.ArgumentParser(
        prog="python -m omnimarket.nodes.node_rsd_migration_orchestrator"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    shape = sub.add_parser("check-shape")
    shape.add_argument("--worktree", type=Path, default=Path())
    shape.add_argument("--node", required=True)
    shape.add_argument("--require-entry-point", action="store_true")
    run = sub.add_parser("run")
    run.add_argument(
        "--task", type=Path, required=True, help="ModelRsdMigrationTask JSON."
    )
    run.add_argument("--state-root", type=Path, required=True)
    run.add_argument("--onex", type=Path, required=True)
    run.add_argument("--delegate-lane", default="dev")
    run.add_argument("--delegate-in-process", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "check-shape":
        problems = canonical_shape_problems(
            args.worktree.resolve(),
            args.node,
            require_entry_point=args.require_entry_point,
        )
        for problem in problems:
            sys.stdout.write(f"{problem}\n")
        sys.stdout.write(f"shape={'ok' if not problems else 'incomplete'}\n")
        return 0 if not problems else 1
    from omnimarket.delegated_code_edit.loop_ports import (
        IN_PROCESS_DELEGATE_FLAGS,
        deployed_lane_delegate_flags,
    )

    task = ModelRsdMigrationTask.model_validate_json(
        args.task.read_text(encoding="utf-8")
    )
    state_root = args.state_root.resolve()
    receipt = HandlerRsdMigrationOrchestrator(
        run_code_edit=_orchestrator_runner(
            state_root=state_root,
            onex=args.onex,
            delegate_flags=(
                IN_PROCESS_DELEGATE_FLAGS
                if args.delegate_in_process
                else deployed_lane_delegate_flags(args.delegate_lane)
            ),
        ),
        state_root=state_root,
    ).run(task)
    sys.stdout.write(json.dumps(receipt.model_dump(mode="json"), separators=(",", ":")))
    sys.stdout.write("\n")
    return 0 if receipt.status == "migrated" else 1


__all__ = ["main"]
