# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_plan_owner_check_effect``: does a live claim own the plan?

Reads the ``open-claims`` answer through ``node_work_ledger_reader_effect`` (the bus-fed
projection) ONCE, decides each finding (``--finding``, or every rule of the overlay when none
is named) with ``HandlerPlanOwnerCheck`` and prints each verdict's line on stdout and one
``PLAN-OWNER`` line per finding on stderr. The hourly throughput tick prints the verdict word
first on the line: ``MISSING ledger-cutover-owner`` is the tick's signal that no live lane
CLAIM names the cutover plan.

The exit code is 0 when every verdict is OWNED, 2 when any is UNKNOWN (a stop, never MISSING:
a stale or failed read never proves a missing owner) and 1 when MISSING. Never reads a file
of the ledger.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from omnimarket.models.model_work_ledger_reader import (
    EnumReaderQuery,
    ModelReaderRequest,
)
from omnimarket.models.plan_owner_check import (
    EnumPlanOwnerVerdict,
    ModelPlanOwnerRequest,
)
from omnimarket.nodes.node_plan_owner_check_effect.adapters import (
    InMemoryPlanOwnerSink,
    JournalPlanOwnerSink,
)
from omnimarket.nodes.node_plan_owner_check_effect.handlers import HandlerPlanOwnerCheck
from omnimarket.nodes.node_plan_owner_check_effect.overlay import load_overlay
from omnimarket.nodes.node_plan_owner_check_effect.protocols import (
    ProtocolPlanOwnerEventSink,
)
from omnimarket.nodes.node_work_ledger_reader_effect.adapters import (
    InMemoryEventSink,
    build_point_read,
)
from omnimarket.nodes.node_work_ledger_reader_effect.handlers import (
    HandlerWorkLedgerReader,
)
from omnimarket.nodes.node_work_ledger_reader_effect.overlay import (
    load_overlay as load_reader_overlay,
)
from omnimarket.nodes.node_work_ledger_reader_effect.protocols import (
    ProtocolLedgerPointRead,
)


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    point_read: ProtocolLedgerPointRead | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> int:
    parser = argparse.ArgumentParser(prog="node_plan_owner_check_effect")
    parser.add_argument(
        "--finding",
        action="append",
        help="the finding to decide (default: every rule of the overlay)",
    )
    args = parser.parse_args(argv)
    env = os.environ if environ is None else environ
    reader_overlay = load_reader_overlay(env)
    overlay = load_overlay(env)
    reader = HandlerWorkLedgerReader(
        reader_overlay,
        point_read
        if point_read is not None
        else build_point_read(reader_overlay.route, env),
        InMemoryEventSink(),
        clock=clock,
    )
    answer = reader.handle(
        ModelReaderRequest(
            reader=overlay.reader,
            query=EnumReaderQuery.OPEN_CLAIMS,
            window_hours=overlay.claims_window_hours,
        )
    )
    journal = env.get(overlay.journal_env, "").strip()
    sink: ProtocolPlanOwnerEventSink = (
        JournalPlanOwnerSink(Path(journal)) if journal else InMemoryPlanOwnerSink()
    )
    handler = HandlerPlanOwnerCheck(overlay, sink, clock=clock)
    findings = (
        tuple(args.finding)
        if args.finding
        else tuple(rule.finding for rule in overlay.rules)
    )
    exit_code = 0
    for finding in findings:
        verdict = handler.handle(
            ModelPlanOwnerRequest(finding=finding, node_answer=answer)
        )
        sys.stdout.write(verdict.line() + "\n")
        sys.stderr.write(verdict.log_line() + "\n")
        if verdict.verdict is EnumPlanOwnerVerdict.UNKNOWN:
            exit_code = 2
        elif verdict.verdict is EnumPlanOwnerVerdict.MISSING and exit_code == 0:
            exit_code = 1
    return exit_code
