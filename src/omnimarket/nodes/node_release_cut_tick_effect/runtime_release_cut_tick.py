# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run one release cut tick from a scheduled unit.

``python -m omnimarket.nodes.node_release_cut_tick_effect`` with ``OMNI_HOME`` (or ``--omni-home``)
naming the registry root and ``ONEX_LEDGER_PATH`` (an absolute path, no default) naming the rolling
work ledger. A status row is appended by ``--ledger-lock`` (a script run as
``<python> <script> <ledger> --append <row>``) or, when that is not given, by the command line in
``ONEX_LEDGER_COMMAND``. ``ONEX_RELEASE_CUT_TICKET`` optionally names the ticket cell of the row.
Exit codes: 0 the tick ran (blocked included; the run record says which), 2 usage.
"""

from __future__ import annotations

import argparse
import os
import shlex
import sys

from omnimarket.nodes.node_release_cut_tick_effect.handlers.handler_release_cut_tick import (
    HandlerReleaseCutTick,
)
from omnimarket.nodes.node_release_cut_tick_effect.models.model_release_cut_tick import (
    ModelReleaseCutTickRequest,
)

LEDGER_VARIABLE = "ONEX_LEDGER_PATH"
LEDGER_COMMAND_VARIABLE = "ONEX_LEDGER_COMMAND"
TICKET_VARIABLE = "ONEX_RELEASE_CUT_TICKET"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Cut an omnimarket release when it is ready, and block by name when it is not"
    )
    parser.add_argument(
        "--omni-home",
        dest="registry_root",
        metavar="OMNI_HOME",
        default=os.environ.get("OMNI_HOME"),
    )
    parser.add_argument("--ledger")
    parser.add_argument("--ledger-lock")
    parser.add_argument("--state-dir")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--osascript", default="/usr/bin/osascript")
    parser.add_argument("--now", help="UTC timestamp used instead of the current time")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="decide and print; dispatch nothing and write no row, state or notification",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.registry_root:
        sys.stderr.write("omnimarket-release-cut: OMNI_HOME is not set\n")
        return 2
    configured = os.environ.get(LEDGER_VARIABLE, "").strip()
    if not args.ledger and not os.path.isabs(configured):
        sys.stderr.write(
            f"omnimarket-release-cut: set {LEDGER_VARIABLE} to the absolute path of the "
            "rolling work ledger (no default)\n"
        )
        return 2
    request = ModelReleaseCutTickRequest(
        registry_root=args.registry_root,
        ledger_path=args.ledger or configured,
        ledger_lock=args.ledger_lock,
        ledger_command=tuple(shlex.split(os.environ.get(LEDGER_COMMAND_VARIABLE, ""))),
        state_dir=args.state_dir,
        python=args.python,
        osascript=args.osascript,
        now=args.now,
        dry_run=args.dry_run,
        ticket=os.environ.get(TICKET_VARIABLE, "").strip(),
    )
    result = HandlerReleaseCutTick().handle(request)
    if result.error_message:
        sys.stderr.write(f"omnimarket-release-cut: {result.error_message}\n")
    sys.stdout.write(result.log_line + "\n")
    return result.exit_code
