# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The command line of the delegation tracker: ``scenarios`` reads the scenario workflows' runs, ``routes`` reads the delegations per hour by route, ``build`` writes the tracker, ``run`` does all three."""

from __future__ import annotations

import argparse
import sys

from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_floor_delegation_effect.handlers.handler_floor_delegation import (
    HandlerFloorDelegation,
)
from omnimarket.nodes.node_floor_delegation_effect.models.model_floor_delegation import (
    ModelFloorDelegationRequest,
)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="floor_delegation")
    ap.add_argument("stage", choices=("scenarios", "routes", "build", "run"))
    ap.add_argument("--home", "--omni-home", dest="home", help="the registry home")
    ap.add_argument(
        "--state-dir",
        help="accepted for the module tick; this node keeps its files under the registry home",
    )
    args = ap.parse_args(argv)
    try:
        result = HandlerFloorDelegation().handle(
            ModelFloorDelegationRequest(stage=args.stage, home=args.home)
        )
    except (OSError, NodeOverlayError, ValidationError) as exc:
        sys.stderr.write(
            f"REFUSED floor-delegation {args.stage}: {type(exc).__name__}: {exc}\n"
        )
        return 2
    sys.stdout.write(result.model_dump_json(indent=1) + "\n")
    return 0 if result.ok else 2
