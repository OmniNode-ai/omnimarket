# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The dispatch handler of the host drift orchestrator: one lab work unit per declared host (OMN-24197)."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from omnimarket.models.model_host_drift_orchestrator import (
    EnumHostDriftHostVerdict,
    ModelHostDriftHostRow,
    ModelHostDriftHostVerdict,
    ModelHostDriftOrchestratorOverlay,
)
from omnimarket.models.model_lab_work_unit import (
    EnumLabWorkUnitStatus,
    ModelLabWorkUnitReceipt,
    ModelLabWorkUnitRequest,
)
from omnimarket.nodes.node_host_drift_orchestrator.handlers.protocols import (
    ProtocolHostDriftLabWorkSender,
)

#: Exit code of the check when the host has drifted (runtime_host_drift_check returns 1 for drift).
EXIT_DRIFTED = 1


class DispatchHostDriftUnits:
    """Sends the overlay's drift check to each host as one lab work unit and types each host's answer."""

    def __init__(
        self,
        overlay: ModelHostDriftOrchestratorOverlay,
        sender: ProtocolHostDriftLabWorkSender,
        new_id: Callable[[], str] = lambda: uuid.uuid4().hex[:16],
    ) -> None:
        self._overlay = overlay
        self._sender = sender
        self._new_id = new_id

    def build_request(self, row: ModelHostDriftHostRow) -> ModelLabWorkUnitRequest:
        lab_work = self._overlay.lab_work
        argv = [
            word.replace("{host}", row.name)
            .replace("{registry_root}", row.registry_root)
            .replace("{profiles_path}", row.profiles_path)
            for word in self._overlay.check.argv
        ]
        return ModelLabWorkUnitRequest(
            work_unit_id=f"{lab_work.unit_id_prefix}-{self._new_id()}",
            target_host=row.name,
            repo=lab_work.repo,
            commit_sha=lab_work.commit_sha,
            argv=argv,
            kind=self._overlay.check.kind,
            timeout_seconds=self._overlay.result_timeout_seconds,
            lane=lab_work.lane,
        )

    def _process(self, row: ModelHostDriftHostRow) -> ModelHostDriftHostVerdict:
        request = self.build_request(row)
        try:
            receipt = self._sender.send(request)
        except Exception as exc:
            return ModelHostDriftHostVerdict(
                host=row.name,
                verdict=EnumHostDriftHostVerdict.NO_RESULT,
                work_unit_id=request.work_unit_id,
                detail=f"{type(exc).__name__}: {exc}"[:2000],
            )
        return verdict_from_receipt(row.name, request.work_unit_id, receipt)

    def dispatch(
        self, hosts: tuple[ModelHostDriftHostRow, ...]
    ) -> tuple[ModelHostDriftHostVerdict, ...]:
        """One verdict per host, in the order the hosts were given."""
        with ThreadPoolExecutor(max_workers=self._overlay.max_parallel_units) as pool:
            return tuple(pool.map(self._process, hosts))


def verdict_from_receipt(
    host: str, work_unit_id: str, receipt: ModelLabWorkUnitReceipt
) -> ModelHostDriftHostVerdict:
    """Type one receipt: an answer needs the check's own VERDICT line, not just an exit code.

    The check exits 1 both for a drifted host and for an uncaught error, so exit 1 reads as drifted
    only with its DRIFTED verdict line; any other exit, or a missing verdict line, is a failed check
    carrying the output tail, which is what names the cause.
    """
    if receipt.status in (
        EnumLabWorkUnitStatus.INFRA_ERROR,
        EnumLabWorkUnitStatus.TIMED_OUT,
    ):
        verdict = EnumHostDriftHostVerdict.NO_RESULT
    elif receipt.status is EnumLabWorkUnitStatus.REFUSED:
        verdict = EnumHostDriftHostVerdict.REFUSED
    elif receipt.exit_code == 0 and f"VERDICT {host}: IN_SYNC" in receipt.output_tail:
        verdict = EnumHostDriftHostVerdict.IN_SYNC
    elif (
        receipt.exit_code == EXIT_DRIFTED
        and f"VERDICT {host}: DRIFTED" in receipt.output_tail
    ):
        verdict = EnumHostDriftHostVerdict.DRIFTED
    else:
        verdict = EnumHostDriftHostVerdict.CHECK_FAILED
    return ModelHostDriftHostVerdict(
        host=host,
        verdict=verdict,
        work_unit_id=work_unit_id,
        exit_code=receipt.exit_code,
        detail=receipt.detail[:2000],
        output_tail=receipt.output_tail[-2000:],
    )
