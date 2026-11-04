# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ask-state command line (OMN-21713). Exit 0 when no ask is dropped, 1 while any is, 2 when a source cannot be read."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from omnimarket.models.model_carry_over import ModelCarryOverRequest
from omnimarket.nodes.node_ask_state_effect.handlers.handler_ask_state_report import (
    AskStateSourceError,
    HandlerAskStateReport,
    load_contract,
    parse_time,
)
from omnimarket.nodes.node_ask_state_effect.handlers.handler_carry_over import (
    HandlerCarryOver,
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
    parser.add_argument(
        "--carry-over",
        type=Path,
        help="JSON cohort with ask_ids, optional evidence and second-engine labels",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="propose and refold; never append to the ledger",
    )
    parser.add_argument(
        "--merged-prs",
        type=Path,
        help="JSON list of PR references measured merged in the watcher",
    )
    args = parser.parse_args(argv)
    if args.carry_over and not args.dry_run:
        parser.error(
            "--carry-over requires --dry-run; apply uses the accepted-ledger node port"
        )
    try:
        since = parse_time(args.since)
        now = parse_time(args.now) if args.now else datetime.now(UTC)
        runtime = load_contract()["runtime"]
        ledger = args.ledger or Path(os.environ[runtime["ledger_env"]])
        captures = args.captures_dir or store_dir()
        read = ModelAskStateReadRequest(
            captures_dir=captures,
            ledger_path=ledger,
            since=since,
            now=now,
            archives=not args.no_archives,
            merged_prs=json.loads(args.merged_prs.read_text())
            if args.merged_prs
            else (),
        )
        if args.carry_over:
            cohort = json.loads(args.carry_over.read_text())
            result = HandlerCarryOver().handle(
                ModelCarryOverRequest.model_validate(
                    {
                        **cohort,
                        "read": read,
                        "dry_run": True,
                    }
                )
            )
            rendered = result.model_dump_json(indent=2)
            text = result.text
            dropped = 0
        else:
            report = HandlerAskStateReport().handle(read)
            rendered = report.model_dump_json(indent=2)
            text = report.text
            dropped = report.counts.dropped
    except KeyError as missing:
        sys.stderr.write(
            f"ask-state: required environment variable {missing} is not set\n"
        )
        return 2
    except (AskStateSourceError, OSError, ValueError) as error:
        sys.stderr.write(f"ask-state: {error}\n")
        return 2
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered + "\n", encoding="utf-8")
    sys.stdout.write((rendered if args.format == "json" else text) + "\n")
    return 1 if dropped else 0


__all__ = ["main"]
