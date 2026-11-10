# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_commitment_writer_effect``: print the ledger row that records an operator's ask.

What the ledger skills call to write a COMMITMENT. The row is built by node_commitment_ledger_compute; this effect reads
the ask and prints the row. The ask is read verbatim from ``--ask-file`` (``-`` is stdin), the row
is printed on stdout and nothing is appended here; the skill appends it through the ledger writer. The rules are the
ledger node's overlay (``ONEX_COMMITMENT_OVERLAY`` pins the file, else ``node_commitment_ledger_compute/overlay.yaml`` under
``ONEX_SKILL_OVERLAY_ROOTS``).

Exit codes: 0 the row is on stdout; 65 the ask, owner or check was refused (each reason on stderr, nothing printed);
3 the overlay is missing.
"""

from __future__ import annotations

import argparse
import secrets
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.models.commitment import (
    ModelCommitmentLedgerRequest,
    ModelCommitmentRules,
)
from omnimarket.nodes.node_commitment_ledger_compute.handlers.handler_commitment_ledger import (
    HandlerCommitmentLedger,
)

LEDGER_NODE = "node_commitment_ledger_compute"
NODE = "node_commitment_writer_effect"
LEDGER_NODE = "node_commitment_ledger_compute"
OVERLAY_ENV = "ONEX_COMMITMENT_OVERLAY"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=f"python -m omnimarket.nodes.{NODE}")
    parser.add_argument("--lane", required=True, help="the lane writing the row")
    parser.add_argument("--owner", default="", help="the lane that owes the ask")
    parser.add_argument(
        "--check", default="", help="the runnable acceptance check, one argv line"
    )
    parser.add_argument("--ask-file", default=None, help="the ask verbatim; - is stdin")
    parser.add_argument("--ticket", default=None)
    parser.add_argument(
        "--requires",
        default="",
        help="comma separated references that must be done first",
    )
    parser.add_argument(
        "--withdraw",
        default=None,
        help="print the row that withdraws this commitment id instead of recording an ask",
    )
    arguments = parser.parse_args(argv)
    if arguments.withdraw is None and arguments.ask_file is None:
        parser.error("--ask-file is required unless --withdraw names a commitment")
    try:
        rules = load_structured_overlay(
            LEDGER_NODE, ModelCommitmentRules, file_env=OVERLAY_ENV
        )
    except NodeOverlayError as exc:
        sys.stderr.write(f"commitment: refused ({type(exc).__name__}: {exc})\n")
        return 3
    if arguments.withdraw is not None:
        request = ModelCommitmentLedgerRequest(
            operation="build_withdraw_row",
            rules=rules,
            now=datetime.now(UTC),
            lane=arguments.lane,
            commitment_id=arguments.withdraw,
        )
    else:
        ask = (
            sys.stdin.read()
            if arguments.ask_file == "-"
            else Path(arguments.ask_file).read_text(encoding="utf-8")
        )
        request = ModelCommitmentLedgerRequest(
            operation="build_open_row",
            rules=rules,
            now=datetime.now(UTC),
            lane=arguments.lane,
            ask=ask.strip(),
            owner=arguments.owner,
            check=arguments.check,
            ticket=arguments.ticket,
            requires=tuple(
                p.strip() for p in arguments.requires.split(",") if p.strip()
            ),
            # One fresh token per ask: two asks of one lane inside a second keep distinct commitment ids.
            nonce=secrets.token_hex(4),
        )
    result = HandlerCommitmentLedger().handle(request)
    if result.row is None:
        for reason in result.refusals:
            sys.stderr.write(f"commitment: refused: {reason}\n")
        return 65
    sys.stdout.write(result.row + "\n")
    return 0


__all__ = ["main"]
