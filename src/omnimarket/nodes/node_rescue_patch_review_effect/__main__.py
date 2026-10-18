# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run one rescue-patch review tick; findings are reported with exit status zero."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from .handlers import HandlerRescuePatchScan
from .models import ModelRescuePatchScanRequest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="onex-rescue-patch-review")
    parser.add_argument(
        "--omni-home", dest="registry_root", default=os.environ.get("OMNI_HOME")
    )
    parser.add_argument(
        "--rescue-root", help="Default: worktree-rescue beside the registry root"
    )
    parser.add_argument(
        "--state-dir",
        default=str(Path.home() / ".local/state/omni/rescue-patch-review"),
    )
    parser.add_argument(
        "--now", default="", help="UTC timestamp used instead of the clock"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.registry_root:
        parser.error("--omni-home is required when OMNI_HOME is not set")
    registry_root = Path(args.registry_root)
    root = (
        Path(args.rescue_root)
        if args.rescue_root
        else registry_root.parent / "worktree-rescue"
    )
    result = HandlerRescuePatchScan().handle(
        ModelRescuePatchScanRequest(
            rescue_root=root,
            registry_root=registry_root,
            state_dir=Path(args.state_dir),
            now=args.now,
        )
    )
    sys.stdout.write(
        f"rescue-patch-review: reviewed={result.reviewed} needs_read={result.needs_read} "
        f"discarded={result.discarded} already_reviewed={result.already_reviewed} "
        f"unjudgeable={result.unjudgeable}\n"
    )
    for line in result.detail:
        sys.stdout.write(f"  {line}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
