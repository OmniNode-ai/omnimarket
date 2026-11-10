# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Command line of the lab run spill node: one action, its result as one JSON line."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from typing import Never

import yaml
from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_lab_run_spill_effect.handlers.handler_lab_run_spill import (
    HandlerLabRunSpill,
    SpillRefusalError,
)
from omnimarket.nodes.node_lab_run_spill_effect.models.model_lab_run_spill import (
    ModelLabRunSpillRequest,
)

EXIT_INDETERMINATE = 3
EXIT_CAPPED = 4


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise ValueError(f"unknown argument: {message}")


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="node_lab_run_spill_effect", description=__doc__)
    parser.add_argument(
        "action",
        choices=("spill_state", "spill_all", "restore", "prune", "preserve_worktree"),
    )
    parser.add_argument("--host", required=True, help="the host's name in the overlay")
    parser.add_argument("--source", required=True)
    parser.add_argument("--spill-root", required=True)
    parser.add_argument("--keep-days", type=int, default=0)
    parser.add_argument("--overlay", default="")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: Sequence[str]) -> int:
    try:
        args = _parser().parse_args(list(argv))
        request = ModelLabRunSpillRequest(
            action=args.action,
            host_name=args.host,
            source=args.source,
            spill_root=args.spill_root,
            overlay_file=args.overlay,
            keep_days=args.keep_days,
            dry_run=args.dry_run,
        )
        result = HandlerLabRunSpill().handle(request)
    except (
        NodeOverlayError,
        ValidationError,
        SpillRefusalError,
        ValueError,
        OSError,
        yaml.YAMLError,
    ) as exc:
        sys.stderr.write(f"INDETERMINATE: {type(exc).__name__}: {exc}\n")
        return EXIT_INDETERMINATE
    sys.stdout.write(result.model_dump_json() + "\n")
    if result.capped:
        return EXIT_CAPPED
    return 1 if result.errors else 0
