# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The command line of the comparison cycle: ``--home`` for the registry home, ``--drill-error`` for the defined fault."""

from __future__ import annotations

import argparse
import sys

from omnimarket.nodes.node_floor_compare_effect.handlers.handler_floor_compare import (
    HandlerFloorCompare,
)
from omnimarket.nodes.node_floor_compare_effect.models.model_floor_compare import (
    ModelFloorCompareRequest,
)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="floor_compare")
    ap.add_argument("--home", "--omni-home", dest="home", help="the registry home")
    ap.add_argument(
        "--state-dir",
        help="accepted for the module tick; this cycle keeps its own state",
    )
    ap.add_argument(
        "--drill-error",
        action="store_true",
        help="run the defined fault and emit its typed failure",
    )
    ap.add_argument(
        "--backfill-only",
        action="store_true",
        help="read the dated copies into the history and record no tick",
    )
    args = ap.parse_args(argv)
    result = HandlerFloorCompare().run(
        ModelFloorCompareRequest(
            home=args.home,
            drill_error=args.drill_error,
            backfill_only=args.backfill_only,
        )
    )
    sys.stdout.write(result.model_dump_json(indent=1) + "\n")
    if result.failure_code == "simple_payload_invalid" and args.drill_error:
        return 0
    return 0 if result.ok else 2
