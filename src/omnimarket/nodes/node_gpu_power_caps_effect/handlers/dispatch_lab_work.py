# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab_work backend's dispatcher: one power caps run as one lab work unit (OMN-24042).

The dispatcher turns a :class:`ModelGpuPowerCapsRequest` into a lab work unit for the overlay's
declared lane, sends it through the injected sender, and maps the receipt back onto a typed
result. The unit's argv runs this node's command line on the host, which prints its own result
as one :data:`RESULT_LINE_PREFIX` line; that line's JSON is what comes back unchanged. Anything
that cannot be sent, answered or read is a refused or failed result that says what is known —
never a raised error and never a guessed reading.
"""

from __future__ import annotations

import uuid

from pydantic import ValidationError

from omnimarket.models.model_lab_work_unit import (
    EnumLabWorkUnitStatus,
    ModelLabWorkUnitReceipt,
    ModelLabWorkUnitRequest,
)
from omnimarket.nodes.node_gpu_power_caps_effect.handlers.protocols import (
    ProtocolLabWorkSender,
)
from omnimarket.nodes.node_gpu_power_caps_effect.models.model_gpu_power_caps import (
    RESULT_LINE_PREFIX,
    EnumGpuPowerCapsOutcome,
    EnumGpuPowerCapsRefusal,
    ModelGpuPowerCapsRequest,
    ModelGpuPowerCapsResult,
)
from omnimarket.nodes.node_gpu_power_caps_effect.models.model_gpu_power_caps_overlay import (
    ModelGpuPowerCapsLabWork,
    ModelGpuPowerCapsOverlay,
)


class DispatchGpuPowerCapsLabWork:
    """Sends one power caps run over the lab work bus and reads the receipt back (OMN-24042)."""

    def __init__(
        self, overlay: ModelGpuPowerCapsOverlay, sender: ProtocolLabWorkSender
    ) -> None:
        self._overlay = overlay
        self._sender = sender

    def dispatch(self, request: ModelGpuPowerCapsRequest) -> ModelGpuPowerCapsResult:
        lab_work = self._overlay.lab_work
        if lab_work is None:
            return self._refused(
                request,
                EnumGpuPowerCapsRefusal.TRANSPORT_UNAVAILABLE,
                "the overlay has no lab_work block",
            )
        if self._overlay.host(request.host) is None:
            return self._refused(
                request,
                EnumGpuPowerCapsRefusal.HOST_UNKNOWN,
                "no row for this host in the overlay",
            )
        unit = ModelLabWorkUnitRequest(
            work_unit_id=f"gpc-{uuid.uuid4().hex[:16]}",
            target_host=request.host,
            repo=lab_work.repo,
            commit_sha=lab_work.commit_sha,
            argv=self._host_argv(lab_work, request),
            kind="other",
            timeout_seconds=lab_work.unit_timeout_seconds,
            lane=lab_work.lane,
        )
        receipt = self._sender.send(unit)
        if receipt.status in (
            EnumLabWorkUnitStatus.INFRA_ERROR,
            EnumLabWorkUnitStatus.REFUSED,
        ):
            return self._refused(
                request, EnumGpuPowerCapsRefusal.TRANSPORT_UNAVAILABLE, receipt.detail
            )
        if receipt.status is EnumLabWorkUnitStatus.TIMED_OUT:
            return self._failed(
                request,
                "the host command timed out; card limits are unknown, read them back",
            )
        # completed: the command ran to an exit code; only its own result line counts.
        parsed = self._parsed_tail(receipt, request)
        if parsed is not None:
            return parsed
        exit_code = "none" if receipt.exit_code is None else str(receipt.exit_code)
        return self._failed(
            request,
            f"the host command ended with exit code {exit_code} and printed no readable result",
        )

    @staticmethod
    def _host_argv(
        lab_work: ModelGpuPowerCapsLabWork, request: ModelGpuPowerCapsRequest
    ) -> list[str]:
        return [
            word.replace("{host}", request.host).replace(
                "{requested_by}", request.requested_by
            )
            for word in lab_work.host_argv
        ]

    @staticmethod
    def _parsed_tail(
        receipt: ModelLabWorkUnitReceipt, request: ModelGpuPowerCapsRequest
    ) -> ModelGpuPowerCapsResult | None:
        """The result of the last result line in the receipt's tail, or None when the tail
        has no readable one for this host."""
        lines = [
            line[len(RESULT_LINE_PREFIX) :]
            for line in receipt.output_tail.splitlines()
            if line.startswith(RESULT_LINE_PREFIX)
        ]
        if not lines:
            return None
        try:
            result = ModelGpuPowerCapsResult.model_validate_json(lines[-1])
        except ValidationError:
            return None
        if result.host != request.host:
            return None
        return result

    @staticmethod
    def _refused(
        request: ModelGpuPowerCapsRequest,
        refusal: EnumGpuPowerCapsRefusal,
        detail: str,
    ) -> ModelGpuPowerCapsResult:
        return ModelGpuPowerCapsResult(
            host=request.host,
            outcome=EnumGpuPowerCapsOutcome.REFUSED,
            refusal=refusal,
            detail=detail,
        )

    @staticmethod
    def _failed(
        request: ModelGpuPowerCapsRequest, detail: str
    ) -> ModelGpuPowerCapsResult:
        return ModelGpuPowerCapsResult(
            host=request.host,
            outcome=EnumGpuPowerCapsOutcome.FAILED,
            detail=detail,
        )
