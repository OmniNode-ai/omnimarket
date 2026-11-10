# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Command line of the parent notice recovery (OMN-23389).

    python -m omnimarket.nodes.node_lane_parent_notice_recovery_effect consume --ledger <ledger.md> [--overlay <file>]
    python -m omnimarket.nodes.node_lane_parent_notice_recovery_effect lanes --ledger <ledger.md> [--overlay <file>]

``consume`` runs one pass and prints the result as JSON; it exits 2 when the pass stopped at a notice (a backoff, a
recovery that did not start) or escalated, 3 when the ledger or overlay is unreadable. ``lanes`` prints the lane read,
one ``<lane> <state> <reason>`` line per lane that has a CLAIM; it changes nothing.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pydantic import ValidationError

from omnimarket.nodes.node_lane_parent_notice_recovery_effect.handlers.adapters import (
    FileLedgerLines,
)
from omnimarket.nodes.node_lane_parent_notice_recovery_effect.handlers.handler_notice_recovery import (
    HandlerNoticeRecovery,
    NoticeRefusalError,
    load_overlay,
    read_lane_states,
)
from omnimarket.nodes.node_lane_parent_notice_recovery_effect.models.model_notice_recovery import (
    EnumNoticeAction,
    ModelNoticeConsumeRequest,
)

NEEDS_A_PERSON = frozenset(
    {
        EnumNoticeAction.ESCALATED,
        EnumNoticeAction.DISPATCH_FAILED,
        EnumNoticeAction.DEFERRED,
    }
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="node_lane_parent_notice_recovery_effect")
    sub = parser.add_subparsers(dest="verb", required=True)
    for verb in ("consume", "lanes"):
        one = sub.add_parser(verb)
        one.add_argument("--ledger", required=True)
        one.add_argument("--overlay", default="")
    args = parser.parse_args(argv)
    try:
        overlay = load_overlay(args.overlay)
        ledger = FileLedgerLines(Path(args.ledger))
        if args.verb == "lanes":
            for lane in read_lane_states(ledger.read_lines(), overlay.failure_statuses):
                sys.stdout.write(
                    f"{lane.lane} {lane.state.value} {lane.reason}".rstrip() + "\n"
                )
            return 0
        result = HandlerNoticeRecovery(
            ledger=ledger, overlay=overlay, real=True
        ).handle(
            ModelNoticeConsumeRequest(
                overlay_file=args.overlay, ledger_file=args.ledger
            )
        )
    except (OSError, ValidationError, NoticeRefusalError) as exc:
        sys.stderr.write(f"INDETERMINATE parent notice recovery: {exc}\n")
        return 3
    sys.stdout.write(result.model_dump_json() + "\n")
    held = [o for o in result.outcomes if o.action in NEEDS_A_PERSON]
    for outcome in held:
        sys.stderr.write(
            f"NOTICE-{outcome.action.value.upper()} {outcome.lane}: {outcome.reason}\n"
        )
    return 2 if held else 0
