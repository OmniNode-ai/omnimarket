# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_ask_done_check_effect``: run asks' done checks.

``--ask <id> --said-at <stamp> --check <spec>`` (repeatable, in order); ``--write`` appends each
verdict row through the overlay's append route, else the rows are only printed. Exit 0 when every
check read (TRUE or FALSE), 1 when a check could not read (UNKNOWN) or a row did not land.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime

from omnimarket.nodes.node_ask_done_check_effect.adapters import HttpFloorRead
from omnimarket.nodes.node_ask_done_check_effect.handlers import HandlerAskDoneCheck
from omnimarket.nodes.node_ask_done_check_effect.models import (
    EnumCheckVerdict,
    ModelAskDoneCheckRequest,
    ModelAskToCheck,
)
from omnimarket.nodes.node_ask_done_check_effect.overlay import load_overlay
from omnimarket.nodes.node_ask_done_check_effect.protocols import ProtocolFloorRead
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    build_appender,
    build_row_source,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ProtocolLedgerRowSource,
    ProtocolReadingAppender,
)


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    floor: ProtocolFloorRead | None = None,
    rows: ProtocolLedgerRowSource | None = None,
    appender: ProtocolReadingAppender | None = None,
) -> int:
    parser = argparse.ArgumentParser(prog="node_ask_done_check_effect")
    parser.add_argument("--ask", action="append", required=True)
    parser.add_argument("--said-at", action="append", required=True)
    parser.add_argument("--check", action="append", required=True)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args(argv)
    if not len(args.ask) == len(args.said_at) == len(args.check):
        parser.error("each --ask needs one --said-at and one --check")
    env = os.environ if environ is None else environ
    overlay = load_overlay(env)
    if floor is None:
        url = env.get(overlay.floor.url_env, "").strip()
        if not url:
            parser.error(
                f"{overlay.floor.url_env} is not set; the overlay names it as the floor URL"
            )
        floor = HttpFloorRead(url, overlay.floor.timeout_seconds)
    handler = HandlerAskDoneCheck(
        overlay,
        floor,
        rows if rows is not None else build_row_source(overlay.read, env),
        appender
        if appender is not None
        else build_appender(overlay.append, overlay.check_lane, env),
    )
    result = handler.handle(
        ModelAskDoneCheckRequest(
            asks=tuple(
                ModelAskToCheck(
                    ask_id=ask,
                    said_at=datetime.fromisoformat(said.replace("Z", "+00:00")),
                    done_check=check,
                )
                for ask, said, check in zip(
                    args.ask, args.said_at, args.check, strict=True
                )
            ),
            write=args.write,
        )
    )
    failed = False
    for verdict in result.verdicts:
        sys.stdout.write(verdict.row)
        sys.stderr.write(
            f"ASK-CHECK ask={verdict.ask_id} verdict={verdict.verdict.value} "
            f"reason={verdict.reason} written={verdict.written}\n"
        )
        failed |= (
            verdict.verdict is EnumCheckVerdict.UNKNOWN or verdict.written == "failed"
        )
    return 1 if failed else 0
