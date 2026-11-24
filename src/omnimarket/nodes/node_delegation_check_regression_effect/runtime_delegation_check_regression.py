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
import os
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.nodes.node_delegation_check_regression_effect.adapters.bus_publisher import (
    BusRegressionPublisher,
)
from omnimarket.nodes.node_delegation_check_regression_effect.adapters.change_sources import (
    CompositeChangeSource,
    GitMergeChangeSource,
    LedgerMarkerChangeSource,
)
from omnimarket.nodes.node_delegation_check_regression_effect.adapters.claims import (
    LedgerClaimHolders,
)
from omnimarket.nodes.node_delegation_check_regression_effect.adapters.gateway_runs import (
    GatewayProbeRunSource,
)
from omnimarket.nodes.node_delegation_check_regression_effect.adapters.inmemory_adapters import (
    InMemorySeenStore,
)
from omnimarket.nodes.node_delegation_check_regression_effect.adapters.state_files import (
    FileDevBuildStore,
    FileSeenStore,
)
from omnimarket.nodes.node_delegation_check_regression_effect.handlers.handler_delegation_check_regression import (
    HandlerDelegationCheckRegression,
)
from omnimarket.nodes.node_delegation_check_regression_effect.models.model_regression import (
    ModelProbeRun,
    ModelRegressionScanRequest,
    ModelRegressionSettings,
    ModelWatchedCheck,
)
from omnimarket.nodes.node_delegation_check_regression_effect.protocols import (
    ProtocolProbeRunSource,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    LedgerRouteError,
    build_row_source,
)

OVERLAY_ID = "node_delegation_check_regression_effect"
OVERLAY_FILE_ENV = "ONEX_DELEGATION_CHECK_REGRESSION_OVERLAY"


def load_settings() -> ModelRegressionSettings:
    return load_structured_overlay(
        OVERLAY_ID, ModelRegressionSettings, file_env=OVERLAY_FILE_ENV
    )


class _UntilRunSource:
    """A replay clock over any run source: a run finished after ``until`` has not happened yet."""

    def __init__(self, inner: ProtocolProbeRunSource, until: datetime) -> None:
        self._inner = inner
        self._until = until

    def runs(self, check: ModelWatchedCheck, limit: int) -> tuple[ModelProbeRun, ...]:
        return tuple(r for r in self._inner.runs(check, limit) if r.at <= self._until)


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
    rows = build_row_source(settings.ledger_read)
    changes = CompositeChangeSource(
        [
            LedgerMarkerChangeSource(rows, settings.change_markers),
            GitMergeChangeSource(registry_root, settings.merge_sources),
            FileDevBuildStore(registry_root / settings.dev_builds_relpath),
        ]
    )
    source: ProtocolProbeRunSource = GatewayProbeRunSource(
        settings.gateway_timeout_seconds
    )
    if replay:
        source = _UntilRunSource(source, now)
    seen = (
        InMemorySeenStore()
        if replay
        else FileSeenStore(registry_root / settings.seen_relpath)
    )
    with BusRegressionPublisher(
        "kafka" if bus == "kafka" else "inmemory", settings.bus_lane, registry_root
    ) as publisher:
        handler = HandlerDelegationCheckRegression(
            settings=settings,
            runs=source,
            changes=changes,
            claims=LedgerClaimHolders(rows, now),
            seen=seen,
            publisher=publisher,
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
    args = parser.parse_args(argv)
    if args.verb == "replay" and not args.at:
        parser.error("replay needs --at")
    home = os.environ.get("OMNI_HOME")
    if not home:
        sys.stderr.write("OMNI_HOME is not set\n")
        return 1
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
            registry_root=Path(home),
            bus=args.bus,
        )
    except (NodeOverlayError, LedgerRouteError, OSError, ValueError) as exc:
        sys.stderr.write(
            json.dumps({"refused": type(exc).__name__, "detail": str(exc)[:240]}) + "\n"
        )
        return 1
