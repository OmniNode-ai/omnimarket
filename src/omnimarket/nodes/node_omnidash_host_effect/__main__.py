# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_omnidash_host_effect <verb>``: install, update and inspect the omnidash host."""

from __future__ import annotations

import argparse
import fcntl
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Never

from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.handlers.overlay_keys import resolve_lab_hosts_table
from omnimarket.models.model_omnidash_host import (
    ModelOmnidashHostInstallResult,
    ModelOmnidashHostUpdateCommand,
)

from .handlers.adapter_omnidash_host_install import (
    OmnidashHostInstaller,
    load_overlay,
    load_schedule,
    overlay_file,
)
from .handlers.adapter_omnidash_host_system import SystemOmnidashHostPorts
from .handlers.handler_omnidash_host_effect import (
    HandlerOmnidashHostEffect,
    read_status,
)


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise ValueError(f"unknown argument: {message}")


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(description=__doc__)
    verbs = parser.add_subparsers(dest="verb", required=True, parser_class=_Parser)
    check = verbs.add_parser("check")
    check.add_argument("--against-lab-table", action="store_true")
    for verb in ("install", "update", "run-now", "status"):
        sub = verbs.add_parser(verb)
        sub.add_argument("--host", required=True, help="the host's name in the overlay")
        if verb == "install":
            sub.add_argument("--dry-run", action="store_true")
        if verb == "update":
            sub.add_argument(
                "--pin-sha", default="", help="serve this commit, not the newest tip"
            )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else list(argv)
    try:
        args = _parser().parse_args(arguments)
        schedule = load_schedule()
        overlay = load_overlay()
        installer = OmnidashHostInstaller(
            schedule=schedule,
            overlay=overlay,
            home=Path.home(),
            overlay_path=overlay_file(os.environ) or Path("/nonexistent"),
        )
        if args.verb == "check":
            result = installer.check(
                resolve_lab_hosts_table() if args.against_lab_table else None
            )
        elif args.verb == "install":
            result = installer.install(args.host, dry_run=args.dry_run)
        elif args.verb == "run-now":
            result = installer.run_now(args.host)
        elif args.verb == "status":
            result = _status(installer, args.host)
        else:
            result = _update(installer, args.host, args.pin_sha)
    except (NodeOverlayError, ValidationError, ValueError, OSError) as exc:
        sys.stderr.write(f"INDETERMINATE: {type(exc).__name__}: {exc}\n")
        return 3
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return result.exit_code


def _status(
    installer: OmnidashHostInstaller, host: str
) -> ModelOmnidashHostInstallResult:
    row = installer.row(host)
    if isinstance(row, ModelOmnidashHostInstallResult):
        return row
    return ModelOmnidashHostInstallResult(
        exit_code=0,
        stdout=read_status(Path(row.status_path)).model_dump_json(indent=2) + "\n",
    )


def _update(
    installer: OmnidashHostInstaller, host: str, pin_sha: str
) -> ModelOmnidashHostInstallResult:
    row = installer.row(host)
    if isinstance(row, ModelOmnidashHostInstallResult):
        return row
    Path(row.build_root).mkdir(parents=True, exist_ok=True)
    with open(Path(row.build_root) / "update.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return ModelOmnidashHostInstallResult(
                exit_code=0, stdout="another update tick is running; skipped\n"
            )
        request = ModelOmnidashHostUpdateCommand(
            host=row, schedule=installer.schedule, pin_sha=pin_sha or None
        )
        result = HandlerOmnidashHostEffect(SystemOmnidashHostPorts()).handle(request)
    line = f"{result.outcome}: {result.detail}".rstrip(": ")
    return ModelOmnidashHostInstallResult(
        exit_code=0, stdout=line + "\n" + result.status.model_dump_json() + "\n"
    )


if __name__ == "__main__":
    raise SystemExit(main())
