# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fixtures of the host thermal hold report tests: the example overlay, a fixed clock and the guard's real file shapes."""

from __future__ import annotations

from datetime import UTC, datetime
from importlib.resources import files

import pytest
import yaml

from omnimarket.nodes.node_host_thermal_hold_report_effect.adapters import (
    InMemoryEpisodeStore,
    InMemoryGuardState,
    InMemoryLedgerAppender,
)
from omnimarket.nodes.node_host_thermal_hold_report_effect.handlers import (
    HandlerHostThermalHoldReport,
)
from omnimarket.nodes.node_host_thermal_hold_report_effect.models import (
    EnumGuardReadState,
    ModelGuardRead,
    ModelHostThermalHoldReportRequest,
)
from omnimarket.nodes.node_host_thermal_hold_report_effect.overlay import (
    ModelHostThermalHoldReportOverlay,
)

NODE = "omnimarket.nodes.node_host_thermal_hold_report_effect"

# The guard's state files as the guard writes them (read from the host, h202, 2026-11-15); the seam test pins them.
LATCH = {
    "cap": 0,
    "cpu_c": 90.625,
    "cut": {"pids": [], "scopes": []},
    "dry_run": False,
    "gpu_c": [80.0],
    "host": "h202",
    "hottest_c": 90.625,
    "reset": "remove this file (operator act)",
    "tripped_at": "2026-11-15T01:47:02Z",
}
LAST = {
    "at": "2026-11-15T02:23:02Z",
    "hottest_c": 72.875,
    "level": "ok",
    "status_at": 1794707234.7856767,
}
OUTBOX_ROW = (
    "2026-11-14T22:01:05Z | STATUS | lane=h202-thermal-guard | ticket=OMN-20769 | actor=systemd:onex-h202-thermal-guard"
    " | host=h202 | req=c4671570-cd75-4729-90c1-d794dbfc0eef | thermal level=stop hottest=90.0C cpu=90.0C gpu=[77.0]"
    " warn>85 stop>90 dry_run=False lanes cut: scopes=[] pids=[]; cap set to 0 (cap.json)"
)
NOW = datetime(2026, 11, 15, 3, 0, 0, tzinfo=UTC)


def outbox_row(index: int) -> str:
    """The guard's outbox row with request id number ``index``."""
    return OUTBOX_ROW.replace(
        "c4671570-cd75-4729-90c1-d794dbfc0eef", f"00000000-0000-4000-8000-{index:012d}"
    )


def present(data: dict) -> ModelGuardRead:
    return ModelGuardRead(EnumGuardReadState.PRESENT, dict(data))


ABSENT = ModelGuardRead(EnumGuardReadState.ABSENT)


@pytest.fixture
def overlay() -> ModelHostThermalHoldReportOverlay:
    document = files(NODE).joinpath("overlay.example.yaml").read_text(encoding="utf-8")
    return ModelHostThermalHoldReportOverlay.model_validate(yaml.safe_load(document))


@pytest.fixture
def guard() -> InMemoryGuardState:
    state = InMemoryGuardState()
    state.last = present(LAST)
    return state


@pytest.fixture
def episodes() -> InMemoryEpisodeStore:
    return InMemoryEpisodeStore()


@pytest.fixture
def appender() -> InMemoryLedgerAppender:
    return InMemoryLedgerAppender()


@pytest.fixture
def handler(guard, episodes, appender) -> HandlerHostThermalHoldReport:
    return HandlerHostThermalHoldReport(guard, episodes, appender, lambda: NOW)


@pytest.fixture
def request_for(overlay):
    def make(dry_run: bool = False, **changes) -> ModelHostThermalHoldReportRequest:
        return ModelHostThermalHoldReportRequest(
            host="lab-a",
            overlay=overlay.model_copy(update=changes) if changes else overlay,
            dry_run=dry_run,
        )

    return make
