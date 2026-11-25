# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_delegation_check_regression_effect <verb>``.

``scan``    read every watched check's finished runs now, raise each new green to red transition and each second flap.
``probe``   the same read, writing nothing: no event, no alarm, no fix lane, no seen record.
``replay``  the same scan at a past moment (``--at``): runs finished after it are not read, the seen record is not kept, so the
            events, alarms and fix lanes of a recorded window are raised again, marked late.

Exit codes: 0 nothing raised, 2 at least one regression or flap raised, 3 a check could not be read or a publish failed.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_delegation_check_regression_effect.adapters.ports import (
    open_ports,
)
from omnimarket.nodes.node_delegation_check_regression_effect.handlers.handler_delegation_check_regression import (
    HandlerDelegationCheckRegression,
)
from omnimarket.nodes.node_delegation_check_regression_effect.models.model_regression import (
    ModelRegressionScanRequest,
    ModelRegressionSettings,
)
from omnimarket.nodes.node_delegation_check_regression_effect.overlay import (
    load_settings,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    LedgerRouteError,
)


def run_scan(
    settings: ModelRegressionSettings,
    *,
    now: datetime,
    write: bool,
    replay: bool,
    registry_root: Path,
    bus: str,
    out: TextIO = sys.stdout,
) -> int:
    with open_ports(
        settings, now=now, registry_root=registry_root, bus=bus, replay=replay
    ) as ports:
        handler = HandlerDelegationCheckRegression(
            settings=settings,
            runs=ports.runs,
            changes=ports.changes,
            claims=ports.claims,
            seen=ports.seen,
            publisher=ports.publisher,
        )
        result = handler.handle(ModelRegressionScanRequest(now=now, write=write))
    out.write(result.model_dump_json(indent=1) + "\n")
    if result.unread or result.publish_failures:
        return 3
    raised = bool(result.events) or any(
        a.alarms for a in result.alarms if a.condition == settings.flap_condition
    )
    return 2 if raised else 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="node_delegation_check_regression_effect")
    parser.add_argument("verb", choices=("scan", "probe", "replay"))
    parser.add_argument(
        "--at", help="replay: the ISO-8601 UTC moment the scan is run at"
    )
    parser.add_argument("--bus", choices=("kafka", "inmemory"), default="kafka")
    parser.add_argument(
        "--registry-root",
        required=True,
        type=Path,
        help="the registry root that holds the ledger, the clones and the state directory",
    )
    args = parser.parse_args(argv)
    if args.verb == "replay" and not args.at:
        parser.error("replay needs --at")
    try:
        settings = load_settings()
        now = (
            datetime.fromisoformat(args.at.replace("Z", "+00:00")).astimezone(UTC)
            if args.at
            else datetime.now(UTC)
        )
        return run_scan(
            settings,
            now=now,
            write=args.verb != "probe",
            replay=args.verb == "replay",
            registry_root=args.registry_root,
            bus=args.bus,
        )
    except (NodeOverlayError, LedgerRouteError, OSError, ValueError) as exc:
        sys.stderr.write(
            json.dumps({"refused": type(exc).__name__, "detail": str(exc)[:240]}) + "\n"
        )
        return 1
