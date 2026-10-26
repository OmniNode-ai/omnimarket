# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Definition-B delegation admission over one Agent call (OMN-21097).

Checks run in a fixed order and the first refusal wins:

1. call origin: absent means ``unknown_call_origin``; any origin other than the
   contract's model-raised origin means ``unsupported_path`` (probe row P11d);
2. path: a background call (``run_in_background`` absent, the engine default,
   or true), a fork (``subagent_type: fork``) or a resume (a ``resume`` value)
   is ``unsupported_path``;
3. the agent-model guard's Agent refusals (probe row P4a);
4. the sensitivity gate, then the task classifier, each over
   ``json.dumps(tool_input)`` exactly as omniclaude's delegation hook runner
   flattens the call.

The result carries no prompt text.
"""

from __future__ import annotations

import json
from typing import Final

from pydantic import JsonValue

from omnimarket.handlers.handler_packaged_contract_loader import (
    load_packaged_contract_section,
)
from omnimarket.nodes.node_delegation_admission_compute.handlers.agent_model_guard_checks import (
    guard_refusal,
)
from omnimarket.nodes.node_delegation_admission_compute.handlers.sensitivity_gate import (
    SensitivityGate,
)
from omnimarket.nodes.node_delegation_admission_compute.handlers.task_intent_classifier import (
    TaskClassifier,
)
from omnimarket.nodes.node_delegation_admission_compute.models.model_delegation_admission import (
    EnumAdmissionReason,
    ModelDelegationAdmissionPolicy,
    ModelDelegationAdmissionRequest,
    ModelDelegationAdmissionResult,
)
from omnimarket.nodes.node_lane_model_policy_compute.handlers.handler_lane_model_policy import (
    resolve_lane_model,
)

_UNCLASSIFIED: Final[str] = "unknown"


def effective_admission_policy(
    policy: ModelDelegationAdmissionPolicy,
) -> ModelDelegationAdmissionPolicy:
    """The guard's score/effort rules survive; model recommendations obey the shared ceiling."""

    def allowed(models: tuple[str, ...], band: str | None = None) -> tuple[str, ...]:
        answers = [
            resolve_lane_model(
                requested_engine="claude",
                requested_tier=model,
                lane_kind="claude-workflow",
                band=band,
            )
            for model in models
        ]
        return tuple(
            dict.fromkeys(
                answer.model for answer in answers if answer.model is not None
            )
        )

    return policy.model_copy(
        update={
            "allowed_models": allowed(policy.allowed_models),
            "route_bands": {
                name: band.model_copy(update={"models": allowed(band.models, name)})
                for name, band in policy.route_bands.items()
            },
        }
    )


DEFAULT_ADMISSION_POLICY: Final[ModelDelegationAdmissionPolicy] = (
    effective_admission_policy(
        ModelDelegationAdmissionPolicy.model_validate(
            load_packaged_contract_section(
                "node_delegation_admission_compute", "admission_policy"
            )
        )
    )
)


def _path_refusal(tool_input: dict[str, JsonValue]) -> str | None:
    background = tool_input.get("run_in_background")
    if background is not False:
        return (
            "background Agent call (run_in_background absent or true); "
            "a background call never reaches admission"
        )
    subagent_type = tool_input.get("subagent_type")
    if isinstance(subagent_type, str) and subagent_type.strip() == "fork":
        return "forked Agent call (subagent_type 'fork')"
    resume = tool_input.get("resume")
    if resume is not None and resume != "":
        return "resumed Agent call (resume set)"
    return None


class HandlerDelegationAdmission:
    """Stateless, pure admission compute; the policy is fixed at composition."""

    def __init__(self, policy: ModelDelegationAdmissionPolicy | None = None) -> None:
        self._policy = (
            DEFAULT_ADMISSION_POLICY
            if policy is None
            else effective_admission_policy(policy)
        )
        self._gate = SensitivityGate()
        self._classifier = TaskClassifier()

    def _result(
        self,
        request: ModelDelegationAdmissionRequest,
        *,
        admitted: bool,
        task_type: str,
        reason: EnumAdmissionReason,
        details: tuple[str, ...] = (),
    ) -> ModelDelegationAdmissionResult:
        return ModelDelegationAdmissionResult(
            correlation_id=request.correlation_id,
            admitted=admitted,
            task_type=task_type,
            reason=reason,
            reason_details=details,
            effective_mode=request.effective_mode,
            policy_version=self._policy.policy_version,
        )

    def handle(
        self, request: ModelDelegationAdmissionRequest
    ) -> ModelDelegationAdmissionResult:
        """Decide admission for one Agent call without I/O or mutation."""
        tool_input = request.tool_input
        origin = request.call_origin
        if origin is None:
            return self._result(
                request,
                admitted=False,
                task_type=_UNCLASSIFIED,
                reason=EnumAdmissionReason.UNKNOWN_CALL_ORIGIN,
                details=("call origin not supplied",),
            )
        if origin != self._policy.model_raised_origin:
            return self._result(
                request,
                admitted=False,
                task_type=_UNCLASSIFIED,
                reason=EnumAdmissionReason.UNSUPPORTED_PATH,
                details=(
                    f"call origin plugin={origin.plugin} tier={origin.tier} "
                    "is not model-raised",
                ),
            )
        path_problem = _path_refusal(tool_input)
        if path_problem is not None:
            return self._result(
                request,
                admitted=False,
                task_type=_UNCLASSIFIED,
                reason=EnumAdmissionReason.UNSUPPORTED_PATH,
                details=(path_problem,),
            )
        refusal = guard_refusal(tool_input, self._policy)
        if refusal is not None:
            reason, detail = refusal
            return self._result(
                request,
                admitted=False,
                task_type=_UNCLASSIFIED,
                reason=reason,
                details=(detail,),
            )
        tool_text = json.dumps(tool_input)
        sensitivity = self._gate.check(tool_text)
        if sensitivity.is_sensitive:
            return self._result(
                request,
                admitted=False,
                task_type=_UNCLASSIFIED,
                reason=EnumAdmissionReason.SENSITIVE,
                details=tuple(sensitivity.reasons),
            )
        score = self._classifier.is_delegatable(tool_text)
        return self._result(
            request,
            admitted=score.delegatable,
            task_type=score.classified_intent,
            reason=(
                EnumAdmissionReason.DELEGATABLE
                if score.delegatable
                else EnumAdmissionReason.NOT_DELEGATABLE
            ),
            details=tuple(score.reasons),
        )


__all__ = ["DEFAULT_ADMISSION_POLICY", "HandlerDelegationAdmission"]
