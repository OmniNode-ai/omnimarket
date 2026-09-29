# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run a host reconciliation from a JSON command; events use declared topics."""

import argparse
import sys
from pathlib import Path

from omnimarket.events.worktree_reconcile import ModelWorktreeReconcileCommand
from omnimarket.nodes.node_worktree_reconcile_effect.handlers.adapter_commands import (
    CommandWorktreeDecider,
    CommandWorktreePinner,
    SystemClock,
)
from omnimarket.nodes.node_worktree_reconcile_effect.handlers.adapter_facts import (
    GitWorktreeFactsProbe,
)
from omnimarket.nodes.node_worktree_reconcile_effect.handlers.adapter_publisher import (
    ContractEventPublisher,
    publish_topics,
)
from omnimarket.nodes.node_worktree_reconcile_effect.handlers.adapter_remover import (
    GitWorktreeRemover,
)
from omnimarket.nodes.node_worktree_reconcile_effect.handlers.handler_worktree_reconcile import (
    HandlerWorktreeReconcile,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="path to a JSON command")
    args = parser.parse_args(argv)
    decided, completed = publish_topics()
    handler = HandlerWorktreeReconcile(
        probe=GitWorktreeFactsProbe(),
        decider=CommandWorktreeDecider(),
        pinner=CommandWorktreePinner(),
        remover=GitWorktreeRemover(),
        clock=SystemClock(),
        publisher=ContractEventPublisher(),
        decided_topic=decided,
        completed_topic=completed,
    )
    command = ModelWorktreeReconcileCommand.model_validate_json(
        Path(args.input).read_text(encoding="utf-8")
    )
    result = handler.handle(command)
    sys.stdout.write(result.model_dump_json() + "\n")
    return 1 if result.completed_event.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
