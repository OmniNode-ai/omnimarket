# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The dispatch handler for the host hygiene orchestrator."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from omnimarket.models.model_host_hygiene import ModelHostHygieneDecisionItem
from omnimarket.models.model_host_hygiene_orchestrator import (
    EnumHostHygieneUnitOutcome,
    ModelHostHygieneOrchestratorOverlay,
    ModelHostHygieneUnitResult,
)
from omnimarket.models.model_lab_work_unit import (
    EnumLabWorkUnitStatus,
    ModelLabWorkUnitReceipt,
    ModelLabWorkUnitRequest,
)
from omnimarket.nodes.node_host_hygiene_orchestrator.handlers.protocols import (
    ProtocolHostHygieneLabWorkSender,
)


class DispatchHostHygieneUnits:
    """Dispatches due decision items as lab-work units through a sender."""

    def __init__(
        self,
        overlay: ModelHostHygieneOrchestratorOverlay,
        sender: ProtocolHostHygieneLabWorkSender,
        new_id: Callable[[], str] = lambda: uuid.uuid4().hex[:16],
    ) -> None:
        self._overlay = overlay
        self._sender = sender
        self._new_id = new_id

    def dispatch(
        self, due: tuple[ModelHostHygieneDecisionItem, ...]
    ) -> tuple[ModelHostHygieneUnitResult, ...]:
        lab_work = self._overlay.lab_work
        host_map = {h.name: h for h in self._overlay.hosts}
        cleanup_map = {c.cleanup_id: c for c in self._overlay.cleanups}

        def _build_request(
            item: ModelHostHygieneDecisionItem,
        ) -> ModelLabWorkUnitRequest | None:
            host_row = host_map.get(item.host)
            cleanup = cleanup_map.get(item.cleanup_id)
            if host_row is None or cleanup is None:
                return None
            argv = tuple(
                word.replace("{host}", item.host).replace(
                    "{registry_root}", host_row.registry_root
                )
                for word in cleanup.argv
            )
            return ModelLabWorkUnitRequest(
                work_unit_id=f"{lab_work.unit_id_prefix}-{self._new_id()}",
                target_host=item.host,
                repo=lab_work.repo,
                commit_sha=lab_work.commit_sha,
                argv=list(argv),
                kind=cleanup.kind,
                timeout_seconds=cleanup.timeout_seconds,
                lane=lab_work.lane,
            )

        def _process(item: ModelHostHygieneDecisionItem) -> ModelHostHygieneUnitResult:
            request = _build_request(item)
            if request is None:
                missing = []
                if item.host not in host_map:
                    missing.append(f"host {item.host!r}")
                if item.cleanup_id not in cleanup_map:
                    missing.append(f"cleanup {item.cleanup_id!r}")
                detail = "undeclared " + " and ".join(missing)
                return ModelHostHygieneUnitResult(
                    host=item.host,
                    cleanup_id=item.cleanup_id,
                    work_unit_id="",
                    outcome=EnumHostHygieneUnitOutcome.REFUSED,
                    exit_code=None,
                    detail=detail,
                    output_tail="",
                )
            try:
                receipt = self._sender.send(request)
            except Exception as exc:
                return ModelHostHygieneUnitResult(
                    host=item.host,
                    cleanup_id=item.cleanup_id,
                    work_unit_id=request.work_unit_id,
                    outcome=EnumHostHygieneUnitOutcome.TRANSPORT_UNAVAILABLE,
                    exit_code=None,
                    detail=f"{type(exc).__name__}: {exc}",
                    output_tail="",
                )
            return _map_receipt(item, request, receipt)

        with ThreadPoolExecutor(max_workers=self._overlay.max_parallel_units) as pool:
            results = list(pool.map(_process, due))
        return tuple(results)


def _map_receipt(
    item: ModelHostHygieneDecisionItem,
    request: ModelLabWorkUnitRequest,
    receipt: ModelLabWorkUnitReceipt,
) -> ModelHostHygieneUnitResult:
    if receipt.status is EnumLabWorkUnitStatus.INFRA_ERROR:
        outcome = EnumHostHygieneUnitOutcome.TRANSPORT_UNAVAILABLE
    elif receipt.status is EnumLabWorkUnitStatus.REFUSED:
        outcome = EnumHostHygieneUnitOutcome.REFUSED
    elif receipt.status is EnumLabWorkUnitStatus.TIMED_OUT:
        outcome = EnumHostHygieneUnitOutcome.TIMED_OUT
    elif receipt.status is EnumLabWorkUnitStatus.COMPLETED:
        if receipt.exit_code == 0:
            outcome = EnumHostHygieneUnitOutcome.COMPLETED
        else:
            outcome = EnumHostHygieneUnitOutcome.FAILED
    else:
        outcome = EnumHostHygieneUnitOutcome.FAILED
    return ModelHostHygieneUnitResult(
        host=item.host,
        cleanup_id=item.cleanup_id,
        work_unit_id=request.work_unit_id,
        outcome=outcome,
        exit_code=receipt.exit_code,
        detail=receipt.detail[:2000],
        output_tail=receipt.output_tail[-2000:],
    )
