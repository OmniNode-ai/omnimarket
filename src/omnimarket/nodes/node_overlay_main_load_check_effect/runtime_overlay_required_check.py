# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_overlay_main_load_check_effect added-nodes|list-missing`` (OMN-26490).

``added-nodes`` refuses the nodes a branch adds against a base ref when the overlay they require is not on overlays main.
``list-missing`` lists every node in the tree that requires an overlay overlays main does not hold, with its owner.

``--store-snapshot-root`` names a host's snapshot of the settings store (the root ``node_overlay_store_read_effect`` writes); an
overlay it holds satisfies the requirement beside overlays main. Unnamed, the store is not read and each refusal says so; a named
root that is not a directory is INDETERMINATE, never a silent pass.

Exit codes: 0 nothing refused; 1 a node was refused; 3 a deployment fact is missing (INDETERMINATE).
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from pydantic import ValidationError

from omnimarket.nodes.node_overlay_main_load_check_effect.handlers.adapter_git_overlay_history import (
    NODES_DIR,
    added_node_names,
    first_commit_subject,
    merged_pr_adds_overlay,
)
from omnimarket.nodes.node_overlay_main_load_check_effect.handlers.handler_overlay_required_check import (
    HandlerOverlayRequiredCheck,
    missing_overlays,
    owner_of,
)
from omnimarket.nodes.node_overlay_main_load_check_effect.models.model_overlay_required_check import (
    ModelOverlayRequiredCommand,
)

SUBCOMMANDS = ("added-nodes", "list-missing")
PR_BODY_ENV = "PR_BODY"


def _store_root_or_none(args: argparse.Namespace) -> str | None:
    root = args.store_snapshot_root
    if root is not None and not Path(root).is_dir():
        raise ValueError(f"--store-snapshot-root {root} is not a directory")
    return str(root) if root is not None else None


def _added_nodes(args: argparse.Namespace) -> int:
    repo = Path(args.repo_root)
    store_root = _store_root_or_none(args)
    try:
        nodes = added_node_names(repo, args.base_ref)
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"INDETERMINATE: {type(exc).__name__}: {exc}\n")
        return 3
    result = HandlerOverlayRequiredCheck(merged_pr_adds=merged_pr_adds_overlay).handle(
        ModelOverlayRequiredCommand(
            repo_root=str(repo),
            overlays_root=args.overlays_root,
            overlays_checkout=args.overlays_checkout,
            store_snapshot_root=store_root,
            nodes=nodes,
            pr_body=os.environ.get(PR_BODY_ENV, ""),
        )
    )
    for finding in result.findings:
        sys.stdout.write(f"refused {finding.node}: {finding.detail}\n")
    sys.stdout.write(
        f"overlay-required: added={len(nodes)} refused={len(result.findings)} "
        f"store={'read' if store_root else 'unread'}\n"
    )
    return 0 if result.ok else 1


def _list_missing(args: argparse.Namespace) -> int:
    repo = Path(args.repo_root)
    store_root = _store_root_or_none(args)
    missing = missing_overlays(
        repo,
        Path(args.overlays_root),
        store_snapshot_root=Path(store_root) if store_root else None,
    )
    for requirement in missing:
        owner = owner_of(
            repo / NODES_DIR / requirement.node,
            lambda node: first_commit_subject(repo, node),
        )
        sys.stdout.write(f"{requirement.node} | {requirement.overlay_id} | {owner}\n")
    sys.stdout.write(
        f"overlay-required: missing={len(missing)} store={'read' if store_root else 'unread'}\n"
    )
    return 0


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="overlay-required", description=__doc__)
    parser.add_argument("command", choices=SUBCOMMANDS)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--overlays-root", required=True)
    parser.add_argument("--overlays-checkout")
    parser.add_argument("--store-snapshot-root")
    parser.add_argument("--base-ref")
    args = parser.parse_args(list(argv))
    try:
        if args.command == "added-nodes":
            if not args.base_ref:
                parser.error("added-nodes needs --base-ref")
            return _added_nodes(args)
        return _list_missing(args)
    except (OSError, ValidationError, ValueError) as exc:
        sys.stderr.write(f"INDETERMINATE: {type(exc).__name__}: {exc}\n")
        return 3
