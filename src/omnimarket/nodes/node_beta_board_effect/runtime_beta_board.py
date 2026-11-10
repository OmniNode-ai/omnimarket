# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The command line of the beta board: ``build`` computes the board, ``publish`` serves it, ``run`` does both."""

from __future__ import annotations

import argparse
import sys

from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_beta_board_effect.handlers.handler_beta_board import (
    HandlerBetaBoard,
)
from omnimarket.nodes.node_beta_board_effect.models.model_beta_board import (
    ModelBetaBoardRequest,
)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="beta_board")
    ap.add_argument("stage", choices=("build", "publish", "run"))
    ap.add_argument("--home", "--omni-home", dest="home", help="the registry home")
    ap.add_argument(
        "--state-dir",
        help="accepted for the module tick; this node keeps its files under the registry home",
    )
    args = ap.parse_args(argv)
    try:
        result = HandlerBetaBoard().handle(
            ModelBetaBoardRequest(stage=args.stage, home=args.home)
        )
    except (OSError, NodeOverlayError, ValidationError) as exc:
        sys.stderr.write(
            f"REFUSED beta-board {args.stage}: {type(exc).__name__}: {exc}\n"
        )
        return 2
    sys.stdout.write(result.model_dump_json(indent=1) + "\n")
    return 0 if result.ok else 2
