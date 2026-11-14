# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The dispatch of the convergence orchestrator: one lab work unit per declared host (OMN-24198)."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from omnimarket.models.model_convergence_orchestrator import (
    EnumConvergenceHostOutcome,
    ModelConvergenceCloneFinding,
    ModelConvergenceHostResult,
    ModelConvergenceHostRow,
    ModelConvergenceOrchestratorOverlay,
)
from omnimarket.models.model_lab_work_unit import (
    EnumLabWorkUnitStatus,
    ModelLabWorkUnitReceipt,
    ModelLabWorkUnitRequest,
)
from omnimarket.nodes.node_convergence_orchestrator.handlers.parse_host_output import (
    host_outcome,
    parse_findings,
)
from omnimarket.nodes.node_convergence_orchestrator.handlers.protocols import (
    ProtocolConvergenceLabWorkSender,
)


class DispatchConvergenceUnits:
    """Sends the overlay's mode command to every declared host and returns one typed result per host."""

    def __init__(
        self,
        overlay: ModelConvergenceOrchestratorOverlay,
        sender: ProtocolConvergenceLabWorkSender,
        new_id: Callable[[], str] = lambda: uuid.uuid4().hex[:16],
    ) -> None:
        self._overlay = overlay
        self._sender = sender
        self._new_id = new_id

    def request_for(self, row: ModelConvergenceHostRow) -> ModelLabWorkUnitRequest:
        lab_work = self._overlay.lab_work
        command = self._overlay.command
        argv = [
            word.replace("{host}", row.name).replace(
                "{registry_root}", row.registry_root
            )
            for word in command.argv
        ]
        return ModelLabWorkUnitRequest(
            work_unit_id=f"{lab_work.unit_id_prefix}-{self._new_id()}",
            target_host=row.name,
            repo=lab_work.repo,
            commit_sha=lab_work.commit_sha,
            argv=argv,
            kind=command.kind,
            timeout_seconds=command.timeout_seconds,
            lane=lab_work.lane,
        )

    def dispatch(self) -> tuple[ModelConvergenceHostResult, ...]:
        with ThreadPoolExecutor(max_workers=self._overlay.max_parallel_units) as pool:
            return tuple(pool.map(self._process, self._overlay.hosts))

    def _process(self, row: ModelConvergenceHostRow) -> ModelConvergenceHostResult:
        request = self.request_for(row)
        try:
            receipt = self._sender.send(request)
        except Exception as exc:
            return self._result(
                row,
                request,
                EnumConvergenceHostOutcome.TRANSPORT_UNAVAILABLE,
                detail=f"{type(exc).__name__}: {exc}",
            )
        return self._map_receipt(row, request, receipt)

    def _result(
        self,
        row: ModelConvergenceHostRow,
        request: ModelLabWorkUnitRequest,
        outcome: EnumConvergenceHostOutcome,
        *,
        exit_code: int | None = None,
        clones: tuple[ModelConvergenceCloneFinding, ...] = (),
        detail: str = "",
        output_tail: str = "",
    ) -> ModelConvergenceHostResult:
        return ModelConvergenceHostResult(
            host=row.name,
            mode=self._overlay.mode,
            work_unit_id=request.work_unit_id,
            outcome=outcome,
            exit_code=exit_code,
            clones=clones,
            detail=detail[:2000],
            output_tail=output_tail[-2000:],
        )

    def _map_receipt(
        self,
        row: ModelConvergenceHostRow,
        request: ModelLabWorkUnitRequest,
        receipt: ModelLabWorkUnitReceipt,
    ) -> ModelConvergenceHostResult:
        status = receipt.status
        if status is EnumLabWorkUnitStatus.INFRA_ERROR:
            outcome = EnumConvergenceHostOutcome.TRANSPORT_UNAVAILABLE
        elif status is EnumLabWorkUnitStatus.TIMED_OUT:
            outcome = EnumConvergenceHostOutcome.TIMED_OUT
        elif status is EnumLabWorkUnitStatus.REFUSED:
            outcome = EnumConvergenceHostOutcome.FAILED
        else:
            findings = parse_findings(receipt.output_tail)
            outcome, why = host_outcome(self._overlay.mode, receipt.exit_code, findings)
            return self._result(
                row,
                request,
                outcome,
                exit_code=receipt.exit_code,
                clones=findings,
                detail=why or receipt.detail,
                output_tail=receipt.output_tail,
            )
        return self._result(
            row,
            request,
            outcome,
            exit_code=receipt.exit_code,
            detail=receipt.detail or f"lab work unit {status.value}",
            output_tail=receipt.output_tail,
        )
