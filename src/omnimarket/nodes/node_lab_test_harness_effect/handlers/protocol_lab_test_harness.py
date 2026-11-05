# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The two ports of the harness effect: where a case runs, and where the reading is kept."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import Protocol

from omnimarket.models.lab_test_harness import (
    ModelLabTestHarnessCase,
    ModelLabTestHarnessOverlay,
    ModelLabTestHarnessReading,
)
from omnimarket.nodes.node_lab_work_unit_effect.models import ModelLabWorkUnitReceipt


class ProtocolLabTestRunner(Protocol):
    """Runs one case as a lab work unit and returns its receipt."""

    async def run(
        self, case: ModelLabTestHarnessCase, *, work_unit_id: str, lane: str
    ) -> ModelLabWorkUnitReceipt: ...


class ProtocolLabTestRunnerOpener(Protocol):
    """Opens the runner for one harness run and closes it after."""

    def __call__(
        self, overlay: ModelLabTestHarnessOverlay
    ) -> AbstractAsyncContextManager[ProtocolLabTestRunner]: ...


class ProtocolLabTestHarnessStore(Protocol):
    """Keeps the current reading where the floor reads it."""

    def write(self, reading: ModelLabTestHarnessReading) -> None: ...

    def read(self) -> ModelLabTestHarnessReading | None: ...
