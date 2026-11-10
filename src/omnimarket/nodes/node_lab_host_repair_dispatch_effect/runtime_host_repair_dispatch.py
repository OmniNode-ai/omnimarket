# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Command line of the host repair dispatch: one watermark reading in, one decision line out (OMN-23389).

    python -m omnimarket.nodes.node_lab_host_repair_dispatch_effect run --reading-file <reading.json> [--overlay <file>]

Exit 0 when nothing needs a person (dispatched, deduped, cleared, ignored), 2 when the node escalated or a dispatch
did not start (``measure-failed``, ``dispatch-failed``, ``escalated``), 3 when the reading or overlay is unreadable.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pydantic import ValidationError

from omnimarket.nodes.node_lab_host_repair_dispatch_effect.handlers.handler_host_repair_dispatch import (
    HandlerHostRepairDispatch,
    RepairRefusalError,
)
from omnimarket.nodes.node_lab_host_repair_dispatch_effect.models.model_host_repair import (
    EnumHostRepairAction,
    ModelHostDiskAlarm,
    ModelHostRepairRequest,
)

NEEDS_A_PERSON = frozenset(
    {
        EnumHostRepairAction.ESCALATED,
        EnumHostRepairAction.MEASURE_FAILED,
        EnumHostRepairAction.DISPATCH_FAILED,
    }
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="node_lab_host_repair_dispatch_effect")
    sub = parser.add_subparsers(dest="verb", required=True)
    run = sub.add_parser("run")
    run.add_argument("--reading-file", required=True)
    run.add_argument("--overlay", default="")
    run.add_argument(
        "--ledger", default="", help="the ledger the started lane writes to"
    )
    args = parser.parse_args(argv)
    try:
        alarm = ModelHostDiskAlarm.model_validate_json(
            Path(args.reading_file).read_text(encoding="utf-8")
        )
        result = HandlerHostRepairDispatch(real=True).handle(
            ModelHostRepairRequest(
                alarm=alarm, overlay_file=args.overlay, ledger_file=args.ledger
            )
        )
    except (OSError, ValidationError, RepairRefusalError) as exc:
        sys.stderr.write(f"INDETERMINATE host repair dispatch: {exc}\n")
        return 3
    sys.stdout.write(result.model_dump_json() + "\n")
    if result.action in NEEDS_A_PERSON:
        sys.stderr.write(
            f"REPAIR-{result.action.value.upper()} {result.host_name}: {result.reason}\n"
        )
        return 2
    return 0
