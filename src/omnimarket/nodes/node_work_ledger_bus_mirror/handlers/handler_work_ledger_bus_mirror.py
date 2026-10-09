# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Map a delegation terminal to one canonical append request."""

from datetime import UTC
from typing import Literal
from urllib.parse import quote
from uuid import NAMESPACE_URL, uuid5

from omnimarket.models.delegation.delegation_caller_lane import caller_lane_refusal
from omnimarket.models.delegation.delegation_ticket_id import ticket_id_refusal
from omnimarket.models.delegation.wire.model_delegate_skill_terminal_projection import (
    ModelDelegateSkillTerminalProjection,
)
from omnimarket.models.work_ledger_append import ModelWorkLedgerAppendRequest


class HandlerWorkLedgerBusMirror:
    """Definition-B compute: no file, clock, bus or model calls."""

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["COMPUTE"]:
        return "COMPUTE"

    def handle(
        self, request: ModelDelegateSkillTerminalProjection
    ) -> ModelWorkLedgerAppendRequest:
        # The run, rather than the delivery envelope or time, is the identity.
        # Both terminal topics share it, including re-enveloped redeliveries.
        request_id = uuid5(
            NAMESPACE_URL,
            f"onex:work-ledger:delegation:{request.correlation_id}",
        )
        lane = request.caller_lane
        if lane is None or caller_lane_refusal(lane) is not None:
            lane = "delegation"
        stamp = request.emitted_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        cells = [
            stamp,
            "STATUS",
            f"lane={lane}",
            "src=delegation",
            f"run={request.correlation_id}",
            f"model={quote(request.model_name, safe='-_.:/') or 'unreported'}",
            f"outcome={request.status}",
            f"quality_gate_passed={str(request.quality_gate_passed).lower()}",
            f"req={request_id}",
            "via=bus:delegation",
        ]
        if request.ticket_id and ticket_id_refusal(request.ticket_id) is None:
            cells.append(f"ticket={request.ticket_id}")
        cells.append("Delegation terminal observed")
        return ModelWorkLedgerAppendRequest(
            request_id=request_id,
            rows=" | ".join(cells),
            requested_by_lane=lane,
            requesting_host="delegation-bus",
            requested_at=request.emitted_at,
        )
