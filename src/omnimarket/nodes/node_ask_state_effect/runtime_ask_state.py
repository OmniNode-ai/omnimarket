# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ask-state command line (OMN-21713). Exit 0 when no ask is dropped, 1 while any is, 2 when a source cannot be read."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from omnimarket.nodes.node_ask_state_effect.handlers.handler_ask_state_report import (
    AskStateSourceError,
    HandlerAskStateReport,
    load_contract,
    parse_time,
)
from omnimarket.nodes.node_ask_state_effect.models.model_ask_state_read import (
    ModelAskStateReadRequest,
)
from omnimarket.nodes.node_operator_capture_effect.handlers.capture_store import (
    store_dir,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="node_ask_state_effect",
        description="Each operator ask or idea with its state; exits 1 while any is dropped.",
    )
    parser.add_argument(
        "--since", required=True, help="UTC stamp, e.g. 2026-10-31T00:00:00Z"
    )
    parser.add_argument("--now", help="judge at this UTC stamp instead of the clock")
    parser.add_argument(
        "--ledger", type=Path, help="the ledger file; default $ONEX_LEDGER_PATH"
    )
    parser.add_argument(
        "--captures-dir", type=Path, help="the operator capture store directory"
    )
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument(
        "--out", type=Path, help="also write the JSON report to this file"
    )
    parser.add_argument(
        "--no-archives", action="store_true", help="read the live ledger only"
    )
    args = parser.parse_args(argv)
    try:
        since = parse_time(args.since)
        now = parse_time(args.now) if args.now else datetime.now(UTC)
        runtime = load_contract()["runtime"]
        ledger = args.ledger or Path(os.environ[runtime["ledger_env"]])
        captures = args.captures_dir or store_dir()
        report = HandlerAskStateReport().handle(
            ModelAskStateReadRequest(
                captures_dir=captures,
                ledger_path=ledger,
                since=since,
                now=now,
                archives=not args.no_archives,
            )
        )
    except KeyError as missing:
        sys.stderr.write(
            f"ask-state: required environment variable {missing} is not set\n"
        )
        return 2
    except (AskStateSourceError, ValueError) as error:
        sys.stderr.write(f"ask-state: {error}\n")
        return 2
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    sys.stdout.write(
        (report.model_dump_json(indent=2) if args.format == "json" else report.text)
        + "\n"
    )
    return 1 if report.counts.dropped else 0


__all__ = ["main"]
