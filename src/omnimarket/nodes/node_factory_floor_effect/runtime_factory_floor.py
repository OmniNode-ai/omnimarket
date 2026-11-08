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
from omnimarket.nodes.node_factory_floor_effect.handlers.handler_floor_lines import (
    HandlerFloorLines,
)
from omnimarket.nodes.node_factory_floor_effect.models.model_factory_floor_run import (
    ModelFactoryFloorRunRequest,
)
from omnimarket.nodes.node_factory_floor_effect.models.model_floor_lines import (
    ModelFloorLinesRequest,
)
from omnimarket.nodes.node_factory_floor_effect.source_loader import (
    SourceRefusedError,
)
from omnimarket.nodes.node_merged_running_count_effect.runtime_merged_running_count import (
    MergedRunningCountReader,
)
from omnimarket.protocols.protocol_merged_running_count_reader import (
    ProtocolMergedRunningCountReader,
)


def bind_merged_running_reader() -> ProtocolMergedRunningCountReader:
    """The composition point: the command line is the one place the floor meets the count's local-file reader."""
    return MergedRunningCountReader()


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


LINES_OPERATION = "lines"


def parse_lines_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog="factory_floor lines",
        description="Print the line board from the newest record the effect wrote (no recompute, no other read).",
    )
    ap.add_argument("--record", help="read this record file instead of the newest one")
    ap.add_argument(
        "--at", help="read the history snapshot at or before this UTC ISO time"
    )
    ap.add_argument("--now", help="override the clock, UTC ISO (tests only)")
    ap.add_argument(
        "--home", help="the registry home; default: the contract's home_env"
    )
    ap.add_argument(
        "--history-dir",
        help="the directory holding the history subdirectory; default: the contract's history_dir_env",
    )
    return ap.parse_args(argv)


def _utc(text: str | None) -> datetime | None:
    if not text:
        return None
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)


def main_lines(argv: list[str]) -> int:
    a = parse_lines_args(argv)
    try:
        result = HandlerFloorLines().handle(
            ModelFloorLinesRequest(
                record_path=a.record,
                history_at=_utc(a.at),
                now=_utc(a.now),
                home=a.home,
                history_dir=a.history_dir,
            )
        )
    except (SourceRefusedError, NodeOverlayError) as exc:
        sys.stderr.write(f"REFUSED factory-floor lines: {exc}\n")
        return 2
    sys.stdout.write(result.board)
    return result.exit_status


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args and args[0] == LINES_OPERATION:
        return main_lines(args[1:])
    try:
        result = HandlerFactoryFloorEffect(
            merged_running=bind_merged_running_reader()
        ).handle(request_from_args(parse_args(args)))
    except (SourceRefusedError, NodeOverlayError) as exc:
        sys.stderr.write(f"REFUSED factory-floor: {exc}\n")
        return 2
    sys.stdout.write(f"{result.output_path}\n")
    return 0
