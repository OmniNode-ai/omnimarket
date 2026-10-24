# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run one owner-summary command: facts, check, triage or mark-sent."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from .handlers import HandlerOwnerSummary
from .models import ModelOwnerSummaryRequest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="onex-owner-summary")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("facts", "mark-sent"):
        command = sub.add_parser(name)
        command.add_argument(
            "--omni-home", dest="root", default=os.environ.get("OMNI_HOME")
        )
        command.add_argument("--state-file")
        if name == "facts":
            command.add_argument("--ledger", default=os.environ.get("ONEX_LEDGER_PATH"))
            command.add_argument("--watcher-state")
            command.add_argument("--since", default="")
            command.add_argument("--until", default="")
            command.add_argument("--friction-data")
            command.add_argument(
                "--full-list-link",
                default="",
                help="where the full list of what the summary leaves out is published",
            )
            command.add_argument(
                "--now", default="", help="UTC instant used instead of the clock"
            )
        else:
            command.add_argument("--until", required=True)
    check = sub.add_parser("check")
    check.add_argument("--draft", required=True)
    check.add_argument("--facts", dest="facts_file", required=True)
    triage = sub.add_parser("triage")
    triage.add_argument("--draft", required=True)
    triage.add_argument("--facts", dest="facts_file", required=True)
    triage.add_argument("--findings", required=True)
    return parser


def _path(value: str | None) -> Path | None:
    return Path(value) if value else None


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    root = getattr(args, "root", None)
    if args.command not in {"check", "triage"} and not root:
        parser.error("--omni-home is required when OMNI_HOME is not set")
    request = ModelOwnerSummaryRequest(
        command=args.command,
        root=Path(root or "."),
        ledger=_path(getattr(args, "ledger", None)),
        watcher_state=_path(getattr(args, "watcher_state", None)),
        state_file=_path(getattr(args, "state_file", None)),
        since=getattr(args, "since", ""),
        until=getattr(args, "until", ""),
        friction_data=_path(getattr(args, "friction_data", None)),
        draft=_path(getattr(args, "draft", None)),
        facts_file=_path(getattr(args, "facts_file", None)),
        findings=_path(getattr(args, "findings", None)),
        now=getattr(args, "now", ""),
        full_list_link=getattr(args, "full_list_link", ""),
    )
    result = HandlerOwnerSummary().handle(request)
    if result.facts is not None:
        sys.stdout.write(result.facts.model_dump_json(indent=1) + "\n")
    for problem in result.problems:
        sys.stdout.write(
            f"{problem}\n" if args.command == "triage" else f"FAIL {problem}\n"
        )
    if result.facts is None:
        sys.stdout.write(f"{result.message}\n")
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
