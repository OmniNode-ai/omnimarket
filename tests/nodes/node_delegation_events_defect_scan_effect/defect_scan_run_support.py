# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Builders for the delegation_events defect scan effect tests: the overlay fixture, an in-memory handler and a fake ssh runner."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from omnimarket.models.model_delegation_events_defect_scan import ModelDefectScanFact
from omnimarket.nodes.node_delegation_events_defect_scan_effect.adapters.events_source import (
    InMemoryDefectEventSource,
)
from omnimarket.nodes.node_delegation_events_defect_scan_effect.handlers.handler_delegation_events_defect_scan_run import (
    HandlerDelegationEventsDefectScanRun,
)
from omnimarket.nodes.node_delegation_events_defect_scan_effect.models.model_defect_scan_run import (
    ModelDefectScanCommand,
)
from omnimarket.nodes.node_delegation_events_defect_scan_effect.overlay import (
    ModelDefectScanEffectOverlay,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)
from tests.nodes.node_delegation_events_defect_scan_compute.defect_scan_support import (
    NOW,
    clean_day,
)
from tests.nodes.node_delegation_events_defect_scan_compute.defect_scan_support import (
    overlay as scan_overlay,
)

OVERLAY_FILE = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "overlays"
    / "node_delegation_events_defect_scan_effect"
    / "overlay.yaml"
)


def effect_overlay() -> ModelDefectScanEffectOverlay:
    return ModelDefectScanEffectOverlay.model_validate(
        yaml.safe_load(OVERLAY_FILE.read_text(encoding="utf-8"))
    )


def handler(
    *,
    events: Sequence[ModelDefectScanFact] | None = None,
    error: str | None = None,
    ledger: InMemoryLedger | None = None,
    write: bool = True,
    now: datetime = NOW,
) -> tuple[HandlerDelegationEventsDefectScanRun, InMemoryLedger]:
    target = ledger if ledger is not None else InMemoryLedger()
    run = HandlerDelegationEventsDefectScanRun(
        scan_overlay=scan_overlay(),
        source=InMemoryDefectEventSource(
            now=None if error else now,
            events=clean_day() if events is None else events,
            error=error,
        ),
        appender=target if write else None,
        clock=lambda: now,
    )
    return run, target


def command(
    write: bool = True, correlation_id: str = "scan-run-1"
) -> ModelDefectScanCommand:
    return ModelDefectScanCommand(correlation_id=correlation_id, write=write)


def answer(
    rows: Sequence[dict[str, Any]], now: str = "2026-11-09T06:15:00.000000+00:00"
) -> str:
    lines = [json.dumps({"now": now}), *(json.dumps(r) for r in rows)]
    return "\n".join(lines) + "\n"


def db_row(
    t: str = "2026-11-09T04:00:00.000000+00:00",
    *,
    src: str = "real",
    shadow: bool = False,
    ok: bool | None = True,
    cost: str | None = "0.0042",
    cms: str | None = "usage-read",
    tok: int = 900,
    lane: str | None = "lane-a",
) -> dict[str, Any]:
    return {
        "t": t,
        "src": src,
        "shadow": shadow,
        "ok": ok,
        "cost": cost,
        "cms": cms,
        "tok": tok,
        "lane": lane,
    }


class FakeRunner:
    """A runner that records the command and the script it was given and returns a canned answer."""

    def __init__(
        self, stdout: str = "", returncode: int = 0, raises: Exception | None = None
    ) -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.raises = raises
        self.calls: list[tuple[list[str], str, int]] = []

    def __call__(
        self, argv: Sequence[str], stdin: bytes | None, timeout: int
    ) -> subprocess.CompletedProcess[bytes]:
        self.calls.append((list(argv), (stdin or b"").decode(), timeout))
        if self.raises is not None:
            raise self.raises
        return subprocess.CompletedProcess(
            list(argv), self.returncode, self.stdout.encode(), b""
        )
