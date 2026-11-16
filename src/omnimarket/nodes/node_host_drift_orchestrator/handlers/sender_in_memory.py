# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The in-memory sender of the host drift orchestrator: it records every unit it is sent."""

from __future__ import annotations

import threading
from collections.abc import Mapping

from omnimarket.models.model_lab_work_unit import (
    EnumLabWorkUnitStatus,
    ModelLabWorkUnitReceipt,
    ModelLabWorkUnitRequest,
)


class InMemoryHostDriftSender:
    """A scripted sender: a host named in ``scripted`` answers as scripted, every other host exits 0.

    ``silent`` hosts send no result: the receipt is the infra_error the lab-work caller returns when no terminal comes.
    """

    def __init__(
        self,
        scripted: Mapping[str, tuple[EnumLabWorkUnitStatus, int | None, str]]
        | None = None,
        silent: frozenset[str] = frozenset(),
    ) -> None:
        self._scripted = scripted or {}
        self._silent = silent
        self._lock = threading.Lock()
        self.sent: list[ModelLabWorkUnitRequest] = []

    def send(self, request: ModelLabWorkUnitRequest) -> ModelLabWorkUnitReceipt:
        with self._lock:
            self.sent.append(request)
        host = request.target_host
        if host in self._silent:
            status, exit_code, tail = EnumLabWorkUnitStatus.INFRA_ERROR, None, ""
            detail = f"no terminal for unit {request.work_unit_id} within {request.timeout_seconds}s"
        else:
            status, exit_code, tail = self._scripted.get(
                host,
                (
                    EnumLabWorkUnitStatus.COMPLETED,
                    0,
                    f"VERDICT {host}: IN_SYNC findings=0",
                ),
            )
            detail = ""
        return ModelLabWorkUnitReceipt(
            work_unit_id=request.work_unit_id,
            target_host=host,
            host="" if host in self._silent else host,
            repo=request.repo,
            commit_sha=request.commit_sha,
            lane=request.lane,
            kind=request.kind,
            status=status,
            exit_code=exit_code,
            log_path="in-memory",
            output_tail=tail,
            detail=detail,
        )
