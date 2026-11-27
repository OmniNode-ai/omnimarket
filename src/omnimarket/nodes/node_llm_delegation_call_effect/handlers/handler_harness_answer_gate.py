# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The skill's quality gate and answer gate, applied to one harness answer (OMN-26219).

The same two steps the local delegation path takes: the contract quality gate, then
the answer gate for the task class. A code class with no named tests is decided by
its contract checks and recorded untested; a text class keeps its contract verdict
while no independent judge is wired, exactly as the local path does.
"""

from __future__ import annotations

from omnibase_core.models.delegation.wire import ModelQualityGateInput

from omnimarket.models.delegation.wire.model_answer_gate_request import (
    EnumAnswerGateKind,
    ModelAnswerGateRequest,
)
from omnimarket.models.delegation.wire.model_bifrost_delegation_config import (
    ModelDelegationBackendConfig,
)
from omnimarket.nodes.node_delegation_answer_gate_compute.handlers.handler_delegation_answer_gate import (
    HandlerDelegationAnswerGate,
    resolve_answer_gate_kind,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    delta as evaluate_quality_gate,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    resolve_task_class_dod_checks,
    resolve_task_class_response_contract,
)
from omnimarket.nodes.node_llm_delegation_call_effect.models.model_harness_backend_call import (
    ModelHarnessBackendCallRequest,
    ModelHarnessGateVerdict,
)


class HandlerHarnessAnswerGate:
    """Judge one harness answer with the delegation gates of its task class."""

    def judge(
        self,
        request: ModelHarnessBackendCallRequest,
        backend: ModelDelegationBackendConfig,
        answer: str,
    ) -> ModelHarnessGateVerdict:
        task_class = request.task_class
        dod_deterministic, dod_heuristic = resolve_task_class_dod_checks(
            task_class, prompt=request.prompt
        )
        contract = evaluate_quality_gate(
            ModelQualityGateInput(
                correlation_id=request.request_id,
                task_type=task_class,
                llm_response_content=answer,
                dod_deterministic=dod_deterministic,
                dod_heuristic=dod_heuristic,
                quality_contract_mode="extend_task_class",
            ),
            response_contract=resolve_task_class_response_contract(task_class),
            grounding_source=request.prompt,
        )
        kind = resolve_answer_gate_kind(task_class)
        if not contract.passed or kind is not EnumAnswerGateKind.CODE:
            return ModelHarnessGateVerdict(
                accepted=contract.passed,
                reasons=tuple(contract.failure_reasons),
                gate_kind=kind.value,
            )
        decided = HandlerDelegationAnswerGate().handle(
            ModelAnswerGateRequest(
                correlation_id=request.request_id,
                task_type=task_class,
                gate_kind=kind,
                producer_model_id=backend.model_name or backend.backend_id,
                contract_gate=contract,
            )
        )
        return ModelHarnessGateVerdict(
            accepted=decided.accepted,
            reasons=tuple(decided.rejection_reasons),
            gate_kind=kind.value,
        )


__all__ = ["HandlerHarnessAnswerGate"]
