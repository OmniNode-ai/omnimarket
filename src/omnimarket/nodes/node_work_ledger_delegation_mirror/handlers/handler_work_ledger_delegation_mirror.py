# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Map a delegation terminal to one canonical append request."""

from datetime import UTC
from typing import Literal
from urllib.parse import quote
from uuid import NAMESPACE_URL, uuid5

from omnimarket.models.delegation.delegation_caller_lane import caller_lane_refusal
from omnimarket.models.delegation.delegation_ticket_id import ticket_id_refusal
from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    resolve_terminal_failure_cause,
)
from omnimarket.models.delegation.wire.model_delegate_skill_terminal_projection import (
    ModelDelegateSkillTerminalProjection,
)
from omnimarket.models.work_ledger_append import ModelWorkLedgerAppendRequest
from omnimarket.nodes.node_work_ledger_delegation_mirror.models.model_delegation_ledger_reason import (
    ModelDelegationLedgerReason,
)


class HandlerWorkLedgerDelegationMirror:
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
        reason = self._reason(request)
        cells = [
            stamp,
            "STATUS",
            f"lane={lane}",
            "src=delegation",
            f"run={request.correlation_id}",
            f"model={quote(request.model_name, safe='-_.:/') or 'unreported'}",
            f"outcome={request.status}",
            f"quality_gate_passed={str(request.quality_gate_passed).lower()}",
            f"reason={quote(reason.reason, safe='-_.:/')}",
            f"reason_source={reason.source}",
            f"req={request_id}",
            "via=bus:delegation",
        ]
        if request.ticket_id and ticket_id_refusal(request.ticket_id) is None:
            cells.append(f"ticket={request.ticket_id}")
        # A returned-to-caller disposition deliberately preserves the caller's
        # route policy. Keep the last attempt's separate evidence visible so
        # quota, missing-credential and gate refusals remain distinguishable.
        if request.status != "completed" and request.attempts:
            last = request.attempts[-1]
            if last.failure_class:
                cells.append(
                    "last_attempt_failure_class="
                    + quote(last.failure_class, safe="-_.:/")
                )
        cells.append("Delegation terminal observed")
        return ModelWorkLedgerAppendRequest(
            request_id=request_id,
            rows=" | ".join(cells),
            requested_by_lane=lane,
            requesting_host="delegation-bus",
            requested_at=request.emitted_at,
        )

    @staticmethod
    def _reason(
        request: ModelDelegateSkillTerminalProjection,
    ) -> ModelDelegationLedgerReason:
        if request.terminal_failure_cause is not None:
            return ModelDelegationLedgerReason(
                reason=request.terminal_failure_cause.value,
                source="terminal_failure_cause",
            )
        if request.terminal_failure_reason:
            return ModelDelegationLedgerReason(
                reason=request.terminal_failure_reason,
                source="terminal_failure_reason",
            )
        if request.status == "timeout":
            return ModelDelegationLedgerReason(reason="timeout", source="status")
        if request.status == "completed" and request.quality_gate_passed:
            return ModelDelegationLedgerReason(reason="none", source="status")
        # Earlier rejected rungs cannot classify a completed run. Reuse the
        # terminal response's resolver for older failed producers that report
        # evidence only on their attempt ladder or outer error.
        if request.status != "completed":
            cause = resolve_terminal_failure_cause(
                request.attempts, error_message=request.error_message
            )
            if cause is not None:
                return ModelDelegationLedgerReason(
                    reason=cause.value, source="failure_evidence"
                )
        if request.quality_gates_failed:
            return ModelDelegationLedgerReason(
                reason="quality_gate_refused", source="quality_gates_failed"
            )
        if request.status == "completed":
            return ModelDelegationLedgerReason(reason="none", source="status")
        return ModelDelegationLedgerReason(reason="unreported", source="unreported")
