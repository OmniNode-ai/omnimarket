# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_factory_floor_effect``: the factory floor command line.

A thin entry: it turns flags into a run request and calls the handler. Where every source is and where the
JSON goes are declared in this node's contract.yaml (``runtime`` block); which plans are lines is the
deployment overlay's ``plan_path_contains`` (or ``--plan``).
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_factory_floor_effect.handlers.handler_factory_floor_effect import (
    HandlerFactoryFloorEffect,
)
from omnimarket.nodes.node_factory_floor_effect.models.model_factory_floor_run import (
    ModelFactoryFloorRunRequest,
)
from omnimarket.nodes.node_factory_floor_effect.source_loader import (
    SourceRefusedError,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog="factory_floor",
        description="Write the assembly-line record of the selected plans to the path the contract declares.",
    )
    ap.add_argument(
        "--plan",
        action="append",
        default=[],
        help="only plans whose path contains this text (repeatable); default: the overlay's plan_path_contains",
    )
    ap.add_argument("--now", help="override the clock, UTC ISO (tests only)")
    ap.add_argument(
        "--ledger-rows-file",
        help="read ledger rows from this file instead of the ledger reader (tests only)",
    )
    ap.add_argument(
        "--home", help="the registry home; default: the contract's home_env"
    )
    ap.add_argument(
        "--kb", help="the plans repository clone; default: the contract's plans_kb_env"
    )
    return ap.parse_args(argv)


def request_from_args(a: argparse.Namespace) -> ModelFactoryFloorRunRequest:
    return ModelFactoryFloorRunRequest(
        plan_path_contains=tuple(a.plan),
        now=datetime.fromisoformat(a.now.replace("Z", "+00:00")).astimezone(UTC)
        if a.now
        else None,
        ledger_rows_file=a.ledger_rows_file,
        home=a.home,
        kb=a.kb,
    )


def main(argv: list[str] | None = None) -> int:
    try:
        result = HandlerFactoryFloorEffect().handle(request_from_args(parse_args(argv)))
    except (SourceRefusedError, NodeOverlayError) as exc:
        sys.stderr.write(f"REFUSED factory-floor: {exc}\n")
        return 2
    sys.stdout.write(f"{result.output_path}\n")
    return 0
