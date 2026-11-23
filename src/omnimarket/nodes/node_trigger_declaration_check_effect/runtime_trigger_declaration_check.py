# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_trigger_declaration_check_effect``: refuse a node that nothing runs.

    --changed-since <ref>   the node contracts a diff from ``<ref>`` to HEAD touches (a file of the node's directory)
    --contract <path>       one repository-relative node contract; repeatable
    --all-nodes             every node contract of the repository

Exit 0: every node that needs a trigger declares one. Exit 1: at least one node declares none (each is printed with its
reason). Exit 2: the diff could not be read. Nothing is passed on a read that failed.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from omnimarket.nodes.node_trigger_declaration_check_effect.handlers import (
    HandlerTriggerDeclarationCheck,
)
from omnimarket.nodes.node_trigger_declaration_check_effect.models import (
    ModelTriggerCheckRequest,
)

NODES_DIR = "src/omnimarket/nodes"
CONTRACT_FILE = "contract.yaml"
_EXIT_REFUSED = 1
_EXIT_UNREADABLE = 2


def changed_node_contracts(root: Path, ref: str) -> tuple[str, ...]:
    """The contract of every node whose directory a diff from ``ref`` (merge base) to HEAD touches and that still exists."""
    done = subprocess.run(
        ["git", "-C", str(root), "diff", "--name-only", f"{ref}...HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    nodes: set[str] = set()
    for line in done.stdout.splitlines():
        parts = Path(line).parts
        if len(parts) > 4 and "/".join(parts[:3]) == NODES_DIR:
            nodes.add(parts[3])
    contracts = (f"{NODES_DIR}/{node}/{CONTRACT_FILE}" for node in sorted(nodes))
    return tuple(path for path in contracts if (root / path).is_file())


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="node_trigger_declaration_check_effect")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--changed-since", metavar="REF")
    parser.add_argument("--contract", action="append", default=[])
    parser.add_argument("--all-nodes", action="store_true")
    args = parser.parse_args(argv)
    contracts = tuple(args.contract)
    if args.changed_since:
        try:
            contracts += changed_node_contracts(args.root, args.changed_since)
        except (OSError, subprocess.CalledProcessError) as exc:
            sys.stderr.write(f"TRIGGER-CHECK unreadable: {exc}\n")
            return _EXIT_UNREADABLE
    result = HandlerTriggerDeclarationCheck().handle(
        ModelTriggerCheckRequest(
            root=str(args.root), contracts=contracts, all_nodes=args.all_nodes
        )
    )
    for finding in result.findings:
        sys.stdout.write(
            f"TRIGGER-CHECK refused {finding.node} [{finding.reason.value}] "
            f"{finding.contract}: {finding.detail}\n"
        )
    sys.stdout.write(
        f"TRIGGER-CHECK checked={result.checked} needing_trigger={result.needing_trigger} "
        f"refused={len(result.findings)}\n"
    )
    return _EXIT_REFUSED if result.findings else 0


if __name__ == "__main__":
    sys.exit(main())
