# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Verify a receipt exchange before applying the existing section 7.2 table."""

from __future__ import annotations

from pydantic import ValidationError

from omnimarket.handlers.handler_packaged_contract_loader import (
    load_packaged_contract_section,
)
from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.models.claude_code_binding import (
    EnumClientMaxMode,
    EnumClientVerdictAction,
)
from omnimarket.models.delegation.model_delegation_client_verdict import (
    EnumDelegationClientOutcome,
    EnumDelegationClientVerdictRow,
    ModelDelegationClientVerdictRequest,
    ModelDelegationClientVerdictResult,
)
from omnimarket.nodes.node_delegation_client_verdict_compute import (
    NodeDelegationClientVerdictCompute,
)
from omnimarket.nodes.node_delegation_receipt_verdict_compute.models.model_client_terminal import (
    ModelClientAdmission,
    ModelClientTerminal,
)
from omnimarket.nodes.node_delegation_receipt_verdict_compute.models.model_receipt_verdict_policy import (
    ModelReceiptVerdictOverlay,
    ModelReceiptVerdictPolicy,
)
from omnimarket.nodes.node_delegation_receipt_verdict_compute.models.model_receipt_verdict_request import (
    ModelReceiptVerdictRequest,
)
from omnimarket.nodes.node_delegation_receipt_verdict_compute.models.model_receipt_verdict_result import (
    ModelReceiptVerdictResult,
)
from omnimarket.nodes.node_delegation_receipt_verdict_compute.protocols.protocol_client_verdict import (
    ProtocolClientVerdict,
)


class HandlerReceiptVerdict:
    """Pure exchange interpretation; topics, allowlist and table are injected."""

    def __init__(
        self,
        *,
        policy: ModelReceiptVerdictPolicy | None = None,
        overlay: ModelReceiptVerdictOverlay | None = None,
        verdict: ProtocolClientVerdict | None = None,
    ) -> None:
        self._policy = (
            policy
            if policy is not None
            else ModelReceiptVerdictPolicy.model_validate(
                load_packaged_contract_section(
                    "node_delegation_receipt_verdict_compute", "receipt_policy"
                )
            )
        )
        self._overlay: ModelReceiptVerdictOverlay | None = None
        self._overlay_error = ""
        try:
            self._overlay = load_structured_overlay(
                "node_delegation_receipt_verdict_compute",
                ModelReceiptVerdictOverlay,
                explicit=overlay,
            )
        except NodeOverlayError as exc:
            self._overlay_error = str(exc)
        self._verdict = (
            verdict if verdict is not None else NodeDelegationClientVerdictCompute()
        )

    @staticmethod
    def _output(
        request: ModelReceiptVerdictRequest,
        verdict: ModelDelegationClientVerdictResult,
        text: str = "",
    ) -> ModelReceiptVerdictResult:
        output = None
        if verdict.action == EnumClientVerdictAction.ANSWER:
            output = {"result": text}
        elif verdict.action == EnumClientVerdictAction.REFUSE:
            output = {"deny": verdict.reason}
        return ModelReceiptVerdictResult(
            correlation_id=request.correlation_id,
            client_verdict=verdict,
            hook_output=output,
        )

    def _refuse(
        self, request: ModelReceiptVerdictRequest, reason: str
    ) -> ModelReceiptVerdictResult:
        verdict = ModelDelegationClientVerdictResult(
            action=EnumClientVerdictAction.REFUSE,
            reason=f"{reason}; correlation_id {request.correlation_id}",
            row=EnumDelegationClientVerdictRow.INCONSISTENT_OBSERVATION,
            mode=request.client_max_mode,
            correlation_id=request.correlation_id,
        )
        return self._output(request, verdict)

    def handle(self, request: ModelReceiptVerdictRequest) -> ModelReceiptVerdictResult:
        """Only configured topics and matching outer and inner identities can answer."""
        overlay = self._overlay
        if overlay is None:
            return self._refuse(request, self._overlay_error)
        admissions: list[ModelClientAdmission] = []
        terminals: list[ModelClientTerminal] = []
        policy = self._policy
        for receipt in request.receipts:
            if receipt.envelope.correlation_id != request.correlation_id:
                continue
            if receipt.topic not in {
                policy.admission_topic,
                policy.completed_topic,
                policy.failed_topic,
            }:
                continue
            if not request.published:
                return self._refuse(request, "receipt contradicts unpublished command")
            try:
                if receipt.topic == policy.admission_topic:
                    admitted = ModelClientAdmission.model_validate(
                        receipt.envelope.payload
                    )
                    if admitted not in admissions:
                        admissions.append(admitted)
                else:
                    terminal = ModelClientTerminal.model_validate(
                        receipt.envelope.payload
                    )
                    if (receipt.topic == policy.completed_topic) != (
                        terminal.status == "completed"
                    ):
                        return self._refuse(
                            request, "terminal topic contradicts status"
                        )
                    if terminal not in terminals:
                        terminals.append(terminal)
            except ValidationError:
                return self._refuse(request, "invalid correlated receipt")
        if len(admissions) > 1 or len(terminals) > 1:
            return self._refuse(request, "conflicting correlated receipts")
        observation = ModelDelegationClientVerdictRequest(
            correlation_id=request.correlation_id,
            client_max_mode=request.client_max_mode,
            outcome=EnumDelegationClientOutcome.ADMISSION_UNKNOWN
            if request.published
            else EnumDelegationClientOutcome.REFUSED_BEFORE_PUBLISH,
            task_type_allowlisted=False,
            client_call_key=request.client_call_key,
            request_fingerprint=request.request_fingerprint,
        )
        if not admissions:
            return self._output(request, self._verdict.handle(observation))
        admission = admissions[0]
        if (
            admission.correlation_id != request.correlation_id
            or admission.client_call_key != request.client_call_key
            or admission.request_fingerprint != request.request_fingerprint
        ):
            return self._refuse(request, "admission identity mismatch")
        outcome = EnumDelegationClientOutcome.TERMINAL_TIMEOUT
        if not admission.admitted:
            outcome = EnumDelegationClientOutcome.NOT_ADMITTED
        elif admission.effective_mode == EnumClientMaxMode.OBSERVE:
            outcome = EnumDelegationClientOutcome.ADMISSION_OBSERVE
        observation = observation.model_copy(
            update={
                "outcome": outcome,
                "effective_mode": admission.effective_mode,
                "admission_reason": admission.reason,
                "task_type_allowlisted": admission.task_type
                in overlay.allowlisted_task_types,
            }
        )
        text = ""
        if terminals:
            terminal = terminals[0]
            if (
                not admission.admitted
                or admission.effective_mode == EnumClientMaxMode.OBSERVE
                or not terminal.admitted
                or terminal.effective_mode != admission.effective_mode
                or terminal.correlation_id != request.correlation_id
                or terminal.task_type != admission.task_type
            ):
                return self._refuse(request, "terminal contradicts admission")
            cause = terminal.terminal_failure_cause
            observation = observation.model_copy(
                update={
                    "outcome": EnumDelegationClientOutcome.TERMINAL,
                    "terminal_status": terminal.status,
                    "terminal_quality_gate_passed": terminal.quality_gate_passed,
                    "terminal_failure_cause": "" if cause is None else cause.value,
                    "echoed_client_call_key": terminal.client_call_key,
                    "echoed_request_fingerprint": terminal.request_fingerprint,
                }
            )
            text = terminal.response
        return self._output(request, self._verdict.handle(observation), text)
