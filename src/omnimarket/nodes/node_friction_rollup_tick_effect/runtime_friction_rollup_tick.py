# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run one friction rollup tick from a launchd unit (OMN-22987).

The host installer's ``registry_module_tick`` runs
``python -m omnimarket.nodes.node_friction_rollup_tick_effect.runtime_friction_rollup_tick
--omni-home <root> --state-dir <evidence dir>`` with ``ONEX_LEDGER_PATH`` (an absolute path, no default) and
``ONEX_LEDGER_COMMAND`` (the proven ``onex-ledger`` command line) in the environment. The reports go under the registry
root, where the factory floor reads ``latest.json``; ``--state-dir`` is only where ``last-run.json`` is written.
``last-run.json`` is written by every tick that finished (state OK, or done with the class rules unapplied) and never by
a refused one, a failed append or a dry run, so a stale file is the fault. Exit codes are the timer's: 0 done, 1 append
refused, 2 usage or unreadable input, 3 done with the class rules unapplied.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from omnimarket.nodes.node_friction_rollup_tick_effect.handlers.friction_rollup_tick_io import (
    write_atomic,
)
from omnimarket.nodes.node_friction_rollup_tick_effect.handlers.handler_friction_rollup_tick import (
    HandlerFrictionRollupTick,
)
from omnimarket.nodes.node_friction_rollup_tick_effect.models.model_friction_rollup_tick import (
    ModelFrictionRollupTickRequest,
)
from omnimarket.nodes.node_friction_rollup_tick_effect.protocols.protocol_ledger_appender import (
    SubprocessLedgerAppender,
)

LEDGER_VARIABLE = "ONEX_LEDGER_PATH"
LEDGER_COMMAND_VARIABLE = "ONEX_LEDGER_COMMAND"
EVIDENCE_FILE = "last-run.json"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="onex-friction-rollup-tick")
    parser.add_argument(
        "--omni-home", dest="registry_root", default=os.environ.get("OMNI_HOME")
    )
    parser.add_argument("--state-dir", help="Where last-run.json is written")
    parser.add_argument("--ledger")
    parser.add_argument("--archive-dir")
    parser.add_argument("--now", help="UTC timestamp used instead of the clock")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.registry_root:
        parser.error("--omni-home is required when OMNI_HOME is not set")
    configured = os.environ.get(LEDGER_VARIABLE, "").strip()
    if not args.ledger and not os.path.isabs(configured):
        parser.error(
            f"set {LEDGER_VARIABLE} to the absolute path of the rolling work ledger (no default)"
        )
    ledger = args.ledger or configured
    handler = HandlerFrictionRollupTick()
    command = shlex.split(os.environ.get(LEDGER_COMMAND_VARIABLE, ""))
    if command:
        settings = handler.settings
        handler = HandlerFrictionRollupTick(
            appender=SubprocessLedgerAppender(
                command, settings.appender_timeout_seconds
            )
        )
    result = handler.handle(
        ModelFrictionRollupTickRequest(
            registry_root=args.registry_root,
            ledger_path=ledger,
            now=args.now,
            dry_run=args.dry_run,
            archive_dir=args.archive_dir,
        )
    )
    if args.dry_run and result.status_row:
        sys.stdout.write(result.status_row + "\n")
    if result.log_line:
        sys.stdout.write(result.log_line + "\n")
    if result.error_message:
        sys.stderr.write(f"friction-rollup: {result.error_message}\n")
    finished = result.state == "OK" and result.exit_code in (0, 3)
    if finished and args.state_dir:
        evidence = {
            "state": result.state,
            "exit_code": result.exit_code,
            "class_rules_applied": result.exit_code == 0,
            "written_at": (
                args.now or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            ),
            "log_line": result.log_line,
        }
        Path(args.state_dir).mkdir(parents=True, exist_ok=True)
        write_atomic(
            Path(args.state_dir) / EVIDENCE_FILE, json.dumps(evidence, indent=2) + "\n"
        )
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
