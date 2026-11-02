# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run the branch prune node once (OMN-21890).

    uv run onex-branch-prune --fixture HOST.json [--dry-run | --execute] [--tips-file F]

The keep policy comes from the node overlay (ONEX_SKILL_OVERLAY_ROOTS). The host
is a JSON fixture: this command makes no GitHub call and deletes no real branch.
Dry run is the default. Prints the full typed result as JSON; exit 0 on a
pruned, dry-run or nothing-to-prune verdict, 1 otherwise.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from omnimarket.nodes.node_branch_prune_effect.handlers.fixture_host import (
    FixtureBranchHost,
    MemoryTipRecord,
)
from omnimarket.nodes.node_branch_prune_effect.handlers.handler_branch_prune import (
    HandlerBranchPrune,
)
from omnimarket.nodes.node_branch_prune_effect.models import (
    EnumBranchPruneVerdict,
    ModelBranchPruneRequest,
)

_OK = {
    EnumBranchPruneVerdict.PRUNED,
    EnumBranchPruneVerdict.DRY_RUN,
    EnumBranchPruneVerdict.NOTHING_TO_PRUNE,
}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="onex-branch-prune")
    p.add_argument("--fixture", required=True, type=Path)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="the default")
    mode.add_argument("--execute", action="store_true")
    p.add_argument("--tips-file", type=Path, default=None)
    args = p.parse_args(argv)

    handler = HandlerBranchPrune(
        host=FixtureBranchHost.from_file(args.fixture),
        tips=MemoryTipRecord(args.tips_file),
    )
    result = handler.handle(ModelBranchPruneRequest(dry_run=not args.execute))
    sys.stdout.write(result.model_dump_json(indent=2) + "\n")
    return 0 if result.verdict in _OK else 1


if __name__ == "__main__":
    raise SystemExit(main())
