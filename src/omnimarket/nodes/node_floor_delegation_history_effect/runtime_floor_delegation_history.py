# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The command line of the delegation history: ``run`` reads the projection, the routing overlay and the reports and writes delegation-history.json."""

from __future__ import annotations

import argparse
import sys

from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_floor_delegation_history_effect.handlers.handler_floor_delegation_history import (
    HandlerFloorDelegationHistory,
)
from omnimarket.nodes.node_floor_delegation_history_effect.models.model_floor_delegation_history import (
    ModelFloorDelegationHistoryRequest,
)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="floor_delegation_history")
    ap.add_argument("stage", choices=("run",))
    ap.add_argument("--home", "--omni-home", dest="home", help="the registry home")
    ap.add_argument(
        "--state-dir",
        help="accepted for the module tick; this node keeps its file under the registry home",
    )
    args = ap.parse_args(argv)
    try:
        result = HandlerFloorDelegationHistory().handle(
            ModelFloorDelegationHistoryRequest(home=args.home)
        )
    except (OSError, NodeOverlayError, ValidationError) as exc:
        sys.stderr.write(
            f"REFUSED floor-delegation-history {args.stage}: {type(exc).__name__}: {exc}\n"
        )
        return 2
    sys.stdout.write(result.model_dump_json(indent=1) + "\n")
    return 0 if result.ok else 2
