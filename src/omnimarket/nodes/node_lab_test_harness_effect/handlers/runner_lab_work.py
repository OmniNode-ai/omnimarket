# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The real runner: each case is a lab work unit sent over the declared bus lane."""

from __future__ import annotations

import contextlib
import inspect
from collections.abc import AsyncIterator
from pathlib import Path

from omnimarket.delegated_test_loop.lane_bus import open_lab_run_bus
from omnimarket.lab_work.bus import LabWorkCaller
from omnimarket.models.lab_test_harness import (
    ModelLabTestHarnessCase,
    ModelLabTestHarnessOverlay,
)
from omnimarket.nodes.node_lab_work_unit_effect.models import (
    ModelLabWorkUnitReceipt,
    ModelLabWorkUnitRequest,
)

# The opener's keyword for the workspace root, read from its signature rather than spelled here.
_WORKSPACE_KW = next(
    name
    for name in inspect.signature(open_lab_run_bus).parameters
    if name.endswith("_home")
)


class LabWorkCallerRunner:
    """Sends a case through ``LabWorkCaller``, the caller behind ``onex lab-work run``."""

    def __init__(self, caller: LabWorkCaller) -> None:
        self._caller = caller

    async def run(
        self, case: ModelLabTestHarnessCase, *, work_unit_id: str, lane: str
    ) -> ModelLabWorkUnitReceipt:
        return await self._caller.dispatch(
            ModelLabWorkUnitRequest(
                work_unit_id=work_unit_id,
                target_host=case.target_host,
                repo=case.repo,
                commit_sha=case.commit_sha,
                argv=list(case.argv),
                kind="test",
                timeout_seconds=case.timeout_seconds,
                lane=lane,
                need_tools=list(case.need_tools),
            )
        )


@contextlib.asynccontextmanager
async def open_lab_work_runner(
    overlay: ModelLabTestHarnessOverlay,
    *,
    workspace_root: Path | None = None,
) -> AsyncIterator[LabWorkCallerRunner]:
    """Open the overlay's bus and a started caller on it; close both on exit."""
    async with open_lab_run_bus(
        bus=overlay.backend,
        lane=overlay.bus_lane,
        kafka_bootstrap=None,
        **{_WORKSPACE_KW: workspace_root},
    ) as bus:
        caller = LabWorkCaller(bus, wait_slack_seconds=overlay.receipt_slack_seconds)
        await caller.start()
        try:
            yield LabWorkCallerRunner(caller)
        finally:
            await caller.stop()
