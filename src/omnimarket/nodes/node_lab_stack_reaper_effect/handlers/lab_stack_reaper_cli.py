# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""lab-stack-reaper: register a lease or run one checked reaper pass."""

from __future__ import annotations

import argparse
import shlex
import signal
from collections.abc import Sequence
from pathlib import Path
from types import FrameType

from ..models import ModelLabStackReaperRequest
from ..models.model_lab_stack_reaper_request import default_lease_dir
from .handler_lab_stack_reaper import HandlerLabStackReaper


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lab-stack-reaper", description=__doc__)
    verbs = parser.add_subparsers(dest="verb", required=True)
    lease = verbs.add_parser(
        "lease", help="register before bringing up or reusing a stack"
    )
    lease.add_argument("--project", required=True)
    lease.add_argument("--lane", required=True)
    lease.add_argument("--claim", required=True)
    lease.add_argument("--generation", default="")
    lease.add_argument("--compose-file", type=Path, action="append", default=[])
    reap = verbs.add_parser("reap", help="one pass, dry run unless --execute")
    reap.add_argument("--execute", action="store_true")
    reap.add_argument("--ledger", type=Path, required=True)
    reap.add_argument("--docker-host")
    reap.add_argument("--retries", type=int, default=3)
    reap.add_argument("--max-ledger-age-s", type=float, default=900)
    reap.add_argument("--interval-min", type=float, default=20)
    reap.add_argument("--cap-min", type=float, default=10)
    reap.add_argument("--deadline-min", type=float, default=30)
    reap.add_argument("--fence-wait-s", type=float, default=30)
    reap.add_argument("--ledger-snapshot-command", default="")
    reap.add_argument("--harnesses", action="store_true")
    reap.add_argument("--ticket-inventory-required", action="store_true")
    reap.add_argument("--worktree-root", type=Path, action="append", default=[])
    reap.add_argument(
        "--ticket", default="none", help="the ticket cell of the rows this pass appends"
    )
    for verb in (lease, reap):
        verb.add_argument(
            "--lease-dir",
            type=Path,
            default=None,
            help="one directory per docker daemon",
        )
        verb.add_argument("--census", type=Path, default=None)
    return parser


def _terminate(signum: int, frame: FrameType | None) -> None:
    raise KeyboardInterrupt


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    common = ModelLabStackReaperRequest(
        args.verb,
        lease_dir=args.lease_dir or default_lease_dir(),
        census=args.census,
        ledger=getattr(args, "ledger", None),
        project=getattr(args, "project", ""),
        lane=getattr(args, "lane", ""),
        claim=getattr(args, "claim", ""),
        generation=getattr(args, "generation", ""),
        compose_files=tuple(getattr(args, "compose_file", [])),
        execute=getattr(args, "execute", False),
        docker_host=getattr(args, "docker_host", None),
        retries=getattr(args, "retries", 3),
        max_ledger_age_s=getattr(args, "max_ledger_age_s", 900),
        interval_min=getattr(args, "interval_min", 20),
        cap_min=getattr(args, "cap_min", 10),
        deadline_min=getattr(args, "deadline_min", 30),
        fence_wait_s=getattr(args, "fence_wait_s", 30),
        ledger_snapshot_command=tuple(
            shlex.split(getattr(args, "ledger_snapshot_command", ""))
        ),
        harnesses=getattr(args, "harnesses", False),
        ticket_inventory_required=getattr(args, "ticket_inventory_required", False),
        worktree_roots=tuple(getattr(args, "worktree_root", [])),
        ticket=getattr(args, "ticket", "none"),
    )
    previous = signal.signal(signal.SIGTERM, _terminate)
    try:
        result = HandlerLabStackReaper().handle(common)
    finally:
        signal.signal(signal.SIGTERM, previous)
    print(result.line())
    if args.verb == "lease" and result.decision == "refused":
        return 3
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
