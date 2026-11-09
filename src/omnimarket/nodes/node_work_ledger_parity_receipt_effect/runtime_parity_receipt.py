# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run the daily work-ledger parity receipt writer from a host unit (OMN-23251).

The host installer's ``registry_module_tick`` runs
``python -m omnimarket.nodes.node_work_ledger_parity_receipt_effect.runtime_parity_receipt --omni-home <root> --state-dir <evidence dir>``
hourly, the ledger path ``--ledger`` or ``ONEX_LEDGER_PARITY_FILE`` (an absolute path, no default), with ``ONEX_LEDGER_COMMAND`` (the proven ``onex-ledger`` command
line) in the environment, plus the four settings the old ledger-publish unit rendered: ``ONEX_LEDGER_PARITY_PYTHON``,
``ONEX_LEDGER_PARITY_OVERLAY``, ``ONEX_LEDGER_PARITY_ENV_FILE`` and ``ONEX_LEDGER_PARITY_PG_PORT``. Exit codes are the
unit's: 0 nothing owed or a receipt appended, 1 failed or not configured, 2 usage.
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

from omnimarket.nodes.node_friction_rollup_tick_effect.protocols.protocol_ledger_appender import (
    SubprocessLedgerAppender,
)
from omnimarket.nodes.node_work_ledger_parity_receipt_effect.handlers.handler_parity_receipt import (
    HandlerParityReceipt,
)
from omnimarket.nodes.node_work_ledger_parity_receipt_effect.models.model_parity_receipt import (
    ModelParityReceiptRequest,
)

LEDGER_VARIABLE = "ONEX_LEDGER_PARITY_FILE"
LEDGER_COMMAND_VARIABLE = "ONEX_LEDGER_COMMAND"
EVIDENCE_FILE = "last-run.json"
PYTHON_VARIABLE = "ONEX_LEDGER_PARITY_PYTHON"
OVERLAY_VARIABLE = "ONEX_LEDGER_PARITY_OVERLAY"
ENV_FILE_VARIABLE = "ONEX_LEDGER_PARITY_ENV_FILE"
PG_PORT_VARIABLE = "ONEX_LEDGER_PARITY_PG_PORT"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="onex-parity-receipt")
    parser.add_argument(
        "--omni-home", dest="registry_root", default=os.environ.get("OMNI_HOME")
    )
    parser.add_argument("--state-dir", help="Where last-run.json is written")
    parser.add_argument(
        "--ledger",
        help="Absolute path of the rolling work ledger; else ONEX_LEDGER_PARITY_FILE (no default)",
    )
    parser.add_argument("--now", help="UTC timestamp used instead of the clock")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.registry_root:
        parser.error("--omni-home is required when OMNI_HOME is not set")
    ledger = args.ledger or os.environ.get(LEDGER_VARIABLE, "").strip()
    if not os.path.isabs(ledger):
        parser.error(
            f"--ledger or {LEDGER_VARIABLE} must be the absolute path of the rolling work ledger (no default)"
        )
    handler = HandlerParityReceipt()
    command = shlex.split(os.environ.get(LEDGER_COMMAND_VARIABLE, ""))
    if command:
        settings = handler.settings
        handler = HandlerParityReceipt(
            appender=SubprocessLedgerAppender(
                command, settings.appender_timeout_seconds
            )
        )
    result = handler.handle(
        ModelParityReceiptRequest(
            registry_root=args.registry_root,
            ledger_path=ledger,
            parity_python=os.environ.get(PYTHON_VARIABLE, ""),
            dsn_overlay_path=os.environ.get(OVERLAY_VARIABLE, ""),
            dsn_env_file=os.environ.get(ENV_FILE_VARIABLE, ""),
            pg_port=os.environ.get(PG_PORT_VARIABLE, ""),
            host_environment=dict(os.environ),
            now=args.now,
            dry_run=args.dry_run,
        )
    )
    sys.stdout.write(result.log_line + "\n")
    if result.error_message:
        sys.stderr.write(f"parity-receipt: {result.error_message}\n")
    if result.exit_code == 0 and args.state_dir and not args.dry_run:
        evidence = {
            "state": result.state,
            "exit_code": result.exit_code,
            "day": result.day,
            "written_at": args.now or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "log_line": result.log_line,
        }
        Path(args.state_dir).mkdir(parents=True, exist_ok=True)
        (Path(args.state_dir) / EVIDENCE_FILE).write_text(
            json.dumps(evidence, indent=2) + "\n", encoding="utf-8"
        )
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
