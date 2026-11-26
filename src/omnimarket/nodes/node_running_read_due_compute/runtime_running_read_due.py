# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_running_read_due_compute <write-due|read-due>``: the due node's call line (OMN-26433).

The node is pure and declares no topic. This is the line a schedule or a merge step runs: it reads its input, calls
``HandlerRunningReadDue.handle`` and prints one JSON object on stdout. It appends nothing; the rows it prints are the
caller's to append through the ledger writer.

The node is pure, so the input arrives on stdin and no file is opened here.

``write-due < merged.json``: the due row one merged pull request maps to (``due_id`` and ``rows``; none for a merge that is
not runtime-affecting).
``read-due [--now ISO] < ledger.md``: the open due rows, the alarm rows owed and the tickets held out of Done, read at
``--now`` (default: the clock), e.g. ``cat "$ONEX_LEDGER_PATH" | python -m ... read-due``.

Exit codes: 0 answered; 2 bad arguments; 3 refused (empty or invalid input, or the node refused the request): the printed
``refusal`` says why, and nothing is answered from an unread ledger.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime

from pydantic import ValidationError

from omnimarket.models.running_read_due import (
    ModelMergedPrFact,
    ModelRunningReadDueRequest,
    ModelRunningReadDueResult,
)
from omnimarket.nodes.node_running_read_due_compute.handlers.handler_running_read_due import (
    HandlerRunningReadDue,
)

EXIT_USAGE = 2
EXIT_REFUSED = 3


def _emit(payload: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")


def _refused(operation: str, refusal: str) -> int:
    _emit(
        {
            "operation": operation,
            "refusal": refusal,
            "open_due_ids": [],
            "alarms": [],
            "blocked_tickets": [],
        }
    )
    return EXIT_REFUSED


def _write_due() -> int:
    try:
        merged = ModelMergedPrFact.model_validate_json(sys.stdin.read())
    except ValidationError as exc:
        return _refused("write_due_row", f"the merge was not read: {exc}"[:300])
    result = HandlerRunningReadDue().handle(
        ModelRunningReadDueRequest(operation="write_due_row", merged=merged)
    )
    _emit(
        {
            "operation": result.operation,
            "due_id": result.due_id,
            "rows": result.append_request.rows if result.append_request else None,
            "refusal": result.refusal,
        }
    )
    return EXIT_REFUSED if result.refusal else 0


def _read_due(now: str | None) -> int:
    text = sys.stdin.read()
    if not text.strip():
        return _refused(
            "read_due_rows", "the ledger text on stdin is empty: nothing was read"
        )
    try:
        moment = datetime.fromisoformat(now) if now else datetime.now(UTC)
    except ValueError as exc:
        return _refused("read_due_rows", f"the time was not read: {exc}"[:300])
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    result: ModelRunningReadDueResult = HandlerRunningReadDue().handle(
        ModelRunningReadDueRequest(
            operation="read_due_rows", ledger_text=text, now=moment
        )
    )
    _emit(
        {
            "operation": result.operation,
            "read_at": moment.isoformat(),
            "open_due_ids": result.open_due_ids,
            "alarms": [
                {
                    "due_id": alarm.due_id,
                    "ticket": alarm.ticket,
                    "owner": alarm.owner,
                    "rows": alarm.append_request.rows,
                }
                for alarm in result.alarms
            ],
            "blocked_tickets": result.blocked_tickets,
            "blocked_reason": result.blocked_reason,
            "refusal": result.refusal,
        }
    )
    return EXIT_REFUSED if result.refusal else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m omnimarket.nodes.node_running_read_due_compute",
        description="The running-read due node's call line: print the due row of a merge, or the due rows owed from a ledger.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser(
        "write-due",
        help="the due row one merged pull request maps to (JSON on stdin)",
    )
    read = sub.add_parser(
        "read-due",
        help="open due rows, alarms owed and tickets held out of Done (ledger text on stdin)",
    )
    read.add_argument("--now", default=None)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return EXIT_USAGE if exc.code else 0
    if args.command == "write-due":
        return _write_due()
    return _read_due(args.now)


__all__: list[str] = ["main"]
