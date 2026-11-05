# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerLabTestHarnessEffect: run every case of the overlay and publish the reading.

One run sends each case to the lab work unit node as a unit, reads the receipts, turns them
into outcomes (a golden case must pass, an error case must go red), computes the reading and
writes it where the floor reads it. A case whose unit did not run (host silent, fetch failed,
timed out) is an infrastructure error and counts against the reading: the harness never
reads a missing receipt as a pass.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime
from typing import cast

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.lab_test_harness import (
    EnumLabTestHarnessKind,
    EnumLabTestHarnessVerdict,
    ModelLabTestHarnessCase,
    ModelLabTestHarnessCaseOutcome,
    ModelLabTestHarnessOverlay,
    ModelLabTestHarnessReading,
    ModelLabTestHarnessRequested,
    ModelLabTestHarnessResult,
    reading_from_result,
)
from omnimarket.nodes.node_lab_test_harness_effect.handlers.protocol_lab_test_harness import (
    ProtocolLabTestHarnessStore,
    ProtocolLabTestRunner,
    ProtocolLabTestRunnerOpener,
)
from omnimarket.nodes.node_lab_test_harness_effect.handlers.runner_lab_work import (
    open_lab_work_runner,
)
from omnimarket.nodes.node_lab_test_harness_effect.handlers.store_lab_test_harness import (
    JsonFileLabTestHarnessStore,
    resolve_result_path,
)
from omnimarket.nodes.node_lab_work_unit_effect.models import (
    EnumLabWorkUnitStatus,
    ModelLabWorkUnitReceipt,
)

OVERLAY_ID = "node_lab_test_harness_effect"


def work_unit_id_for(run_id: str, case_id: str) -> str:
    """A stable unit id per run and case, so a repeated request names the same unit."""
    return "lth-" + hashlib.sha256(f"{run_id}/{case_id}".encode()).hexdigest()[:16]


def outcome_for(
    case: ModelLabTestHarnessCase, work_unit_id: str, receipt: ModelLabWorkUnitReceipt
) -> ModelLabTestHarnessCaseOutcome:
    """Verdict from the receipt, and whether it is what the case's kind requires."""
    if (
        receipt.status is not EnumLabWorkUnitStatus.COMPLETED
        or receipt.exit_code is None
    ):
        verdict = EnumLabTestHarnessVerdict.INFRA_ERROR
    elif receipt.exit_code == 0:
        verdict = EnumLabTestHarnessVerdict.PASS
    else:
        verdict = EnumLabTestHarnessVerdict.FAIL
    required = (
        EnumLabTestHarnessVerdict.PASS
        if case.kind is EnumLabTestHarnessKind.GOLDEN
        else EnumLabTestHarnessVerdict.FAIL
    )
    return ModelLabTestHarnessCaseOutcome(
        case_id=case.case_id,
        kind=case.kind,
        work_unit_id=work_unit_id,
        host=receipt.host,
        verdict=verdict,
        exit_code=receipt.exit_code,
        as_expected=verdict is required,
        duration_seconds=receipt.duration_seconds,
        log_path=receipt.log_path,
        detail=receipt.detail
        or (receipt.output_tail[-300:] if verdict is not required else ""),
    )


class HandlerLabTestHarnessEffect:
    """EFFECT handler: one run request in, the current reading out."""

    def __init__(
        self,
        overlay: ModelLabTestHarnessOverlay | None = None,
        runner_opener: ProtocolLabTestRunnerOpener | None = None,
        store: ProtocolLabTestHarnessStore | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._overlay = overlay
        self._opener = runner_opener
        self._store = store
        self._now = now

    def _settings(self) -> ModelLabTestHarnessOverlay:
        if self._overlay is None:
            self._overlay = load_structured_overlay(
                OVERLAY_ID, ModelLabTestHarnessOverlay
            )
        return self._overlay

    def store(self) -> ProtocolLabTestHarnessStore:
        if self._store is None:
            self._store = JsonFileLabTestHarnessStore(
                resolve_result_path(self._settings().current_result_path)
            )
        return self._store

    async def run_case(
        self,
        runner: ProtocolLabTestRunner,
        case: ModelLabTestHarnessCase,
        run_id: str,
        lane: str,
    ) -> ModelLabTestHarnessCaseOutcome:
        unit_id = work_unit_id_for(run_id, case.case_id)
        receipt = await runner.run(case, work_unit_id=unit_id, lane=lane)
        return outcome_for(case, unit_id, receipt)

    async def handle(
        self, request: ModelLabTestHarnessRequested
    ) -> ModelLabTestHarnessReading:
        overlay = self._settings()
        opener = self._opener or cast(ProtocolLabTestRunnerOpener, open_lab_work_runner)
        started = self._now()
        outcomes: list[ModelLabTestHarnessCaseOutcome] = []
        async with opener(overlay) as runner:
            for case in overlay.cases:
                outcomes.append(
                    await self.run_case(runner, case, request.run_id, overlay.lane)
                )
        finished = self._now()
        result = ModelLabTestHarnessResult(
            run_id=request.run_id,
            piece=overlay.piece,
            tag=overlay.tag,
            overlay_revision=overlay.overlay_revision,
            started_at=started,
            finished_at=finished,
            outcomes=tuple(outcomes),
        )
        reading = reading_from_result(
            result, max_age_seconds=overlay.max_age_seconds, now=finished
        )
        self.store().write(reading)
        return reading


__all__: list[str] = [
    "HandlerLabTestHarnessEffect",
    "outcome_for",
    "work_unit_id_for",
]
