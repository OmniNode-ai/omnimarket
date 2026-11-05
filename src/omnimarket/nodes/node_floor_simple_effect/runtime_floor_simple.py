# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The command line of the 17a cycle: ``--home`` for the registry home, ``--drill-error`` for the defined fault,
``--check-overlays ROOT`` to load the three overlays from a root and say which is invalid (exit 1)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.models.floor_17a import ModelFloorSimpleTickRequest
from omnimarket.nodes.node_floor_simple_effect.handlers.handler_floor_simple_tick import (
    HandlerFloorSimpleTick,
    check_overlays,
)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="floor_simple")
    ap.add_argument("--home", "--omni-home", dest="home", help="the registry home")
    ap.add_argument(
        "--state-dir",
        help="accepted for the module tick; this cycle keeps its own state",
    )
    ap.add_argument(
        "--drill-error",
        action="store_true",
        help="run the defined fault through the real reader and emit its typed failure",
    )
    ap.add_argument(
        "--check-overlays", metavar="ROOT", help="load the overlays under ROOT and exit"
    )
    args = ap.parse_args(argv)
    if args.check_overlays:
        try:
            for line in check_overlays(Path(args.check_overlays)):
                sys.stdout.write(line + "\n")
        except (NodeOverlayError, ValidationError, OSError) as exc:
            sys.stderr.write(f"INVALID overlay: {type(exc).__name__}: {exc}\n")
            return 1
        return 0
    try:
        result = HandlerFloorSimpleTick().run(
            ModelFloorSimpleTickRequest(home=args.home, drill_error=args.drill_error)
        )
    except NodeOverlayError as exc:
        sys.stderr.write(f"REFUSED floor-simple: {exc}\n")
        return 2
    sys.stdout.write(result.model_dump_json(indent=1) + "\n")
    if result.failure_code == "drill_completed":
        return 0
    return 0 if result.ok else 2
