# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The in-memory sender for the convergence orchestrator."""

from __future__ import annotations

from collections.abc import Mapping

from omnimarket.models.model_lab_work_unit import (
    EnumLabWorkUnitStatus,
    ModelLabWorkUnitReceipt,
    ModelLabWorkUnitRequest,
)


class InMemoryConvergenceSender:
    """A scripted in-memory sender that records every request it was sent.

    ``scripted`` maps a host name to the (status, exit code, output) it answers with; a host not named
    answers a completed unit with exit 0 and no output.
    """

    def __init__(
        self,
        scripted: Mapping[str, tuple[EnumLabWorkUnitStatus, int | None, str]]
        | None = None,
    ) -> None:
        self._scripted = scripted or {}
        self.sent: list[ModelLabWorkUnitRequest] = []

    def send(self, request: ModelLabWorkUnitRequest) -> ModelLabWorkUnitReceipt:
        self.sent.append(request)
        host = request.target_host
        status, exit_code, tail = self._scripted.get(
            host, (EnumLabWorkUnitStatus.COMPLETED, 0, "")
        )
        return ModelLabWorkUnitReceipt(
            work_unit_id=request.work_unit_id,
            target_host=host,
            host=host,
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
