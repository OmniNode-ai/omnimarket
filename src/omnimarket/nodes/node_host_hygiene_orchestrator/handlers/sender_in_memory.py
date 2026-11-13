# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The in-memory sender for the host hygiene orchestrator."""

from __future__ import annotations

from collections.abc import Mapping

from omnimarket.models.model_lab_work_unit import (
    EnumLabWorkUnitStatus,
    ModelLabWorkUnitReceipt,
    ModelLabWorkUnitRequest,
)


class InMemoryHostHygieneSender:
    """A scripted in-memory sender that records every request it was sent."""

    def __init__(
        self,
        scripted: Mapping[str, tuple[EnumLabWorkUnitStatus, int | None, str]]
        | None = None,
    ) -> None:
        self._scripted = scripted or {}
        self.sent: list[ModelLabWorkUnitRequest] = []

    def send(self, request: ModelLabWorkUnitRequest) -> ModelLabWorkUnitReceipt:
        self.sent.append(request)
        target_host = request.target_host
        if target_host in self._scripted:
            status, exit_code, tail = self._scripted[target_host]
        else:
            status, exit_code, tail = EnumLabWorkUnitStatus.COMPLETED, 0, "ok"
        return ModelLabWorkUnitReceipt(
            work_unit_id=request.work_unit_id,
            target_host=target_host,
            host=target_host,
            repo=request.repo,
            commit_sha=request.commit_sha,
            lane=request.lane,
            kind=request.kind,
            status=status,
            exit_code=exit_code,
            log_path="in-memory",
            output_tail=tail,
            duration_seconds=0.0,
        )
