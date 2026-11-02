# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run the handoff check node once from a shell (OMN-21910).

    onex-handoff-check assemble     build the handoff; REFUSE (exit 1) when a ruling or consent
                                    in the window has no status; record an unhealthy service,
                                    never refuse
    onex-handoff-check resume       read the newest handoff, re-probe its stale lines, list changes
    onex-handoff-check record-slice record a scrubbed ledger slice as a test fixture

Exit: 0 ok, 1 refused, 2 bad input, 3 no handoff found (resume). The same request goes over
the bus through ``onex run-node node_handoff_check_effect --input <json>``.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from omnimarket.nodes.node_handoff_check_effect.handlers.handler_handoff_check import (
    CONTRACT_PATH,
    HandlerHandoffCheck,
)
from omnimarket.nodes.node_handoff_check_effect.models import (
    EnumHandoffCommand,
    ModelHandoffCheckRequest,
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="onex-handoff-check",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--contract", default=str(CONTRACT_PATH))
    common.add_argument("--ledger", default=os.environ.get("ONEX_LEDGER_PATH"))
    common.add_argument("--archive-dir")
    common.add_argument(
        "--now", help="UTC time to evaluate at (tests); default is the clock"
    )
    common.add_argument(
        "--pr-reads",
        help="recorded pr_state_local.py output instead of running the reader",
    )
    common.add_argument(
        "--probes", help="recorded probe results (JSON) instead of live probes"
    )
    a = sub.add_parser(
        "assemble", parents=[common], help="build and validate the handoff"
    )
    a.add_argument(
        "--out-dir",
        help="directory to write the handoff into (a worktree of the shared document clone, under beta/handoffs)",
    )
    a.add_argument("--session")
    a.add_argument("--lane", help="your lane name, for the inbox reader")
    a.add_argument("--goal-file", help="the operator's words, one line per sentence")
    a.add_argument("--statuses", help="JSON: item -> {status, evidence}")
    a.add_argument("--unverified-file")
    a.add_argument("--limits-file")
    a.add_argument("--plan", action="append", help="plan file (repeatable)")
    a.add_argument("--pr", action="append", help="extra PR, repo#n (repeatable)")
    a.add_argument(
        "--readers",
        action="store_true",
        help="also embed the catchup and inbox readers' output",
    )
    r = sub.add_parser(
        "resume", parents=[common], help="re-probe the newest handoff's stale lines"
    )
    r.add_argument("--kb-clone", required=True, help="the shared document clone")
    r.add_argument("--ref", default="origin/main")
    r.add_argument("--directory")
    s = sub.add_parser(
        "record-slice",
        parents=[common],
        help="record a scrubbed ledger slice as a fixture",
    )
    s.add_argument("--since", required=True)
    s.add_argument("--until", required=True)
    s.add_argument("--out", required=True)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    request = ModelHandoffCheckRequest(
        command=EnumHandoffCommand(args.cmd),
        ledger=args.ledger,
        archive_dir=args.archive_dir,
        now=args.now,
        pr_reads=args.pr_reads,
        probes=args.probes,
        out_dir=getattr(args, "out_dir", None),
        session=getattr(args, "session", None),
        lane=getattr(args, "lane", None),
        goal_file=getattr(args, "goal_file", None),
        statuses=getattr(args, "statuses", None),
        unverified_file=getattr(args, "unverified_file", None),
        limits_file=getattr(args, "limits_file", None),
        plans=tuple(getattr(args, "plan", None) or ()),
        prs=tuple(getattr(args, "pr", None) or ()),
        readers=bool(getattr(args, "readers", False)),
        kb_clone=getattr(args, "kb_clone", None),
        ref=getattr(args, "ref", "origin/main"),
        directory=getattr(args, "directory", None),
        since=getattr(args, "since", None),
        until=getattr(args, "until", None),
        out=getattr(args, "out", None),
    )
    result = HandlerHandoffCheck(Path(args.contract)).handle(request)
    for line in result.output:
        sys.stdout.write(line + "\n")
    for line in result.errors:
        sys.stderr.write(line + "\n")
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
