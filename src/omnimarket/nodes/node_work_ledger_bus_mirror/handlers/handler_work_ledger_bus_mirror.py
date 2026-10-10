# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure lab terminal to STATUS mapping (OMN-20278)."""

from datetime import UTC
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

from omnimarket.models.model_work_ledger_bus_mirror_request import (
    ModelWorkLedgerBusMirrorRequest,
)
from omnimarket.models.work_ledger_append import ModelWorkLedgerAppendRequest


class HandlerWorkLedgerBusMirror:
    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["COMPUTE"]:
        return "COMPUTE"

    def handle(
        self, request: ModelWorkLedgerBusMirrorRequest
    ) -> ModelWorkLedgerAppendRequest:
        # Identity depends only on the unit, never a delivery envelope or clock.
        request_id = uuid5(NAMESPACE_URL, f"onex:lab-work-unit:{request.work_unit_id}")
        lane = request.lane or "lab-work"
        timestamp = request.envelope_timestamp.astimezone(UTC)
        row = " | ".join(
            [
                timestamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "STATUS",
                f"lane={lane}",
                "src=lab-work",
                f"unit={request.work_unit_id}",
                f"host={request.host}",
                f"repo={request.repo or 'none'}",
                f"sha={request.commit_sha or 'none'}",
                f"kind={request.kind}",
                f"state={request.status}",
                f"exit={request.exit_code if request.exit_code is not None else 'none'}",
                f"duration_s={request.duration_seconds:g}",
                f"req={request_id}",
                f"via=bus:{request.host}",
                "Lab work unit terminal received",
            ]
        )
        return ModelWorkLedgerAppendRequest(
            request_id=request_id,
            rows=row,
            requested_by_lane=lane,
            requesting_host=request.host,
            requested_at=timestamp,
        )
