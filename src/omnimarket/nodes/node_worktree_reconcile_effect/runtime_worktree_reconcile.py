# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run a host reconciliation from a JSON command; events use declared topics."""

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

from omnimarket.events.worktree_reconcile import (
    ModelWorktreeReconcileCommand,
    ModelWorktreeReconcileRunResult,
)
from omnimarket.nodes.node_worktree_reconcile_effect.container.container_worktree_reconcile_effect import (
    create_handler_worktree_reconcile,
    create_handler_worktree_schedule,
)
from omnimarket.nodes.node_worktree_reconcile_effect.handlers.adapter_commands import (
    CommandMergedPrReader,
    CommandWorktreeDecider,
    CommandWorktreePinner,
    SystemClock,
)
from omnimarket.nodes.node_worktree_reconcile_effect.handlers.adapter_facts import (
    GitWorktreeFactsProbe,
)
from omnimarket.nodes.node_worktree_reconcile_effect.handlers.adapter_launchd_units import (
    LaunchdUnitRootReader,
)
from omnimarket.nodes.node_worktree_reconcile_effect.handlers.adapter_publisher import (
    ContractEventPublisher,
    publish_topics,
)
from omnimarket.nodes.node_worktree_reconcile_effect.handlers.adapter_remover import (
    GitWorktreeRemover,
)
from omnimarket.nodes.node_worktree_reconcile_effect.handlers.adapter_units import (
    SystemdUnitRootReader,
)


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments and arguments[0] == "schedule":
        from .models.model_worktree_schedule import ModelWorktreeScheduleRequest

        parser = argparse.ArgumentParser()
        parser.add_argument("--input", required=True)
        args = parser.parse_args(arguments[1:])
        request = ModelWorktreeScheduleRequest.model_validate_json(
            Path(args.input).read_text()
        )
        schedule_result = create_handler_worktree_schedule().handle(request)
        sys.stdout.write(schedule_result.model_dump_json() + "\n")
        return schedule_result.exit_code
    if arguments and arguments[0] == "tick":
        return scheduled_tick(arguments[1:])
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="path to a JSON command")
    args = parser.parse_args(argv)
    command = ModelWorktreeReconcileCommand.model_validate_json(
        Path(args.input).read_text(encoding="utf-8")
    )
    result = run_command(command)
    sys.stdout.write(result.model_dump_json() + "\n")
    return 1 if result.completed_event.failures else 0


def run_command(
    command: ModelWorktreeReconcileCommand,
) -> ModelWorktreeReconcileRunResult:
    decided, completed = publish_topics()
    handler = create_handler_worktree_reconcile(
        probe=GitWorktreeFactsProbe(
            unit_reader=(
                LaunchdUnitRootReader()
                if sys.platform == "darwin"
                else SystemdUnitRootReader(
                    declared_environment_files=command.policy.unreadable_environment_files
                )
            ),
            merged_pr_reader=CommandMergedPrReader(),
        ),
        decider=CommandWorktreeDecider(),
        pinner=CommandWorktreePinner(),
        remover=GitWorktreeRemover(),
        clock=SystemClock(),
        publisher=ContractEventPublisher(),
        decided_topic=decided,
        completed_topic=completed,
    )
    return handler.handle(command)


def scheduled_tick(argv: list[str]) -> int:
    import fcntl

    from .handlers.handler_worktree_schedule import load_overlay

    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True)
    parser.add_argument("--overlay", required=True)
    args = parser.parse_args(argv)
    overlay = load_overlay(args.overlay, args.host)
    state = Path(overlay.state_directory)
    state.mkdir(parents=True, exist_ok=True)
    with (state / "tick.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            sys.stdout.write(json.dumps({"status": "LOCKED", "host": args.host}) + "\n")
            return 0
        command = overlay.hosts[args.host].model_copy(
            update={
                "correlation_id": uuid4(),
                "exclude_paths": tuple(
                    set(overlay.hosts[args.host].exclude_paths)
                    | {
                        overlay.project,
                        str(state),
                    }
                ),
            }
        )
        # Use the exact same handler, adapters and contract topics as a manual run.
        result = run_command(command)
        # An interrupted write leaves the preceding complete receipt intact.
        with tempfile.NamedTemporaryFile(mode="w", dir=state, delete=False) as receipt:
            temporary = Path(receipt.name)
            try:
                receipt.write(result.model_dump_json() + "\n")
                receipt.flush()
                os.fsync(receipt.fileno())
                os.replace(temporary, state / "latest.json")
            finally:
                temporary.unlink(missing_ok=True)
        sys.stdout.write(result.model_dump_json() + "\n")
        return 1 if result.completed_event.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
