# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Resolve model policy once for every lane/worker producer (OMN-21396)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from omnimarket.handlers.handler_packaged_contract_loader import (
    load_packaged_contract_section,
)

from ..models.model_lane_model_policy import ModelLaneModelPolicy
from ..models.model_lane_model_policy_request import ModelLaneModelPolicyRequest
from ..models.model_lane_model_policy_result import ModelLaneModelPolicyResult

CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"
POLICY_SOURCE = (
    "omnimarket.nodes.node_lane_model_policy_compute/contract.yaml#lane_model_policy"
)


@lru_cache(maxsize=1)
def load_policy() -> ModelLaneModelPolicy:
    """Missing or malformed declarations refuse; there is no dispatcher fallback."""
    return ModelLaneModelPolicy.model_validate(
        load_packaged_contract_section(
            "node_lane_model_policy_compute", "lane_model_policy"
        )
    )


class HandlerLaneModelPolicy:
    def __init__(self, policy: ModelLaneModelPolicy | None = None) -> None:
        self.policy = load_policy() if policy is None else policy

    def _escalated(self, reason: str | None) -> bool:
        """True for complexity:<band at or above the minimum> or sonnet-failed:<run>."""
        if reason is None:
            return False
        kind, _, value = reason.strip().partition(":")
        if kind == "sonnet-failed":
            return bool(value.strip())
        bands = list(self.policy.band_ceilings)
        floor = bands.index(self.policy.escalation_min_band)
        return kind == "complexity" and value in bands and bands.index(value) >= floor

    def handle(
        self, request: ModelLaneModelPolicyRequest
    ) -> ModelLaneModelPolicyResult:
        p = self.policy
        escalated = self._escalated(request.escalation_reason)
        ceiling = p.escalation_ceiling if escalated else p.ceiling

        def answer(model: str | None, reason: str) -> ModelLaneModelPolicyResult:
            return ModelLaneModelPolicyResult(
                allowed=model is not None,
                model=model,
                engine=p.engines[model] if model is not None else None,
                ceiling=ceiling,
                reason=reason,
                policy_source=POLICY_SOURCE,
            )

        def disabled(model: str) -> str:
            return (
                f"background-{model}-disabled: {p.ruling}; ceiling={ceiling}; "
                "needs a valid escalation_reason"
            )

        def canonical(value: str) -> str:
            return p.aliases.get(
                value.strip().lower().replace("_", "-"), value.strip().lower()
            )

        if request.escalation_reason is not None and not escalated:
            return answer(
                None,
                f"invalid escalation_reason {request.escalation_reason!r}: {p.ruling}",
            )
        for bound in (
            request.route_ceiling,
            p.band_ceilings.get(request.band) if request.band else None,
        ):
            if bound is None:
                continue
            bound = canonical(bound)
            if bound not in p.order:
                return answer(None, f"unknown-ceiling: {bound}")
            ceiling = min((ceiling, bound), key=p.order.index)
        if request.band is not None:
            if request.band not in p.band_ceilings:
                return answer(None, f"unknown-band: {request.band}")
            if p.band_ceilings[request.band] is None:
                return answer(None, f"deterministic-only: {request.band}")
        engine = canonical(request.requested_engine)
        if engine not in (*p.order, "auto", *p.engines.values()):
            return answer(None, f"unknown-engine: {request.requested_engine}")
        family = p.engines.get(engine, engine)
        if request.requested_tier is not None:
            tier = canonical(request.requested_tier)
            if tier not in p.order or (family not in ("auto", p.engines[tier])):
                return answer(
                    None, f"unknown-or-incompatible-tier: {request.requested_tier}"
                )
            family = p.engines[tier]
            # A requested tier is a ceiling; an exact requirement is separate.
            ceiling = min((ceiling, tier), key=p.order.index)
        required_family = None
        if request.required_engine:
            required = canonical(request.required_engine)
            required_family = p.engines.get(required, required)
            if required_family not in p.engines.values():
                return answer(
                    None, f"unknown-required-engine: {request.required_engine}"
                )
            # An explicit model spelling is an exact requirement, not merely a family.
            if required in p.order and p.order.index(required) > p.order.index(ceiling):
                return answer(None, disabled(required))
        exact = canonical(request.required_model) if request.required_model else None
        if exact is not None:
            if exact not in p.order:
                return answer(None, f"unknown-required-model: {request.required_model}")
            if p.order.index(exact) > p.order.index(ceiling):
                return answer(None, disabled(exact))
        available = set(
            p.order
            if request.available_models is None
            else map(canonical, request.available_models)
        )
        if not available <= set(p.order):
            return answer(None, "unknown-capacity-model")
        if request.available_engines is not None:
            if not set(request.available_engines) <= set(p.engines.values()):
                return answer(None, "unknown-capacity-engine")
            available = {
                m for m in available if p.engines[m] in request.available_engines
            }
        low_risk = (request.risk_class or request.lane_kind) in p.low_risk_classes
        for model in p.order[: p.order.index(ceiling) + 1]:
            model_family = p.engines[model]
            if model not in available or (exact and model != exact):
                continue
            if model in p.escalation_models and not escalated:
                continue
            if family != "auto" and model_family != family:
                continue
            if required_family and model_family != required_family:
                continue
            if request.lane_kind in p.claude_only_kinds and model_family != "claude":
                continue
            if model_family in ("code-edit", "glm") and not low_risk:
                continue
            if model_family == "code-edit" and not request.code_edit_ready:
                continue
            return answer(model, "allowed")
        return answer(None, f"no-allowed-model: {p.ruling}; ceiling={ceiling}")


def resolve_lane_model(**facts: object) -> ModelLaneModelPolicyResult:
    """In-process entry point used by producers, including the retirement adapters."""
    return HandlerLaneModelPolicy().handle(
        ModelLaneModelPolicyRequest.model_validate(facts)
    )


def require_lane_model(**facts: object) -> str:
    """For a producer that must stop rather than dispatch without a policy answer."""
    decision = resolve_lane_model(**facts)
    if decision.model is None:
        raise ValueError(decision.reason)
    return decision.model


def lane_model_refusal(
    model: str,
    *,
    lane_kind: str = "small-fix",
    escalation_reason: str | None = None,
) -> str | None:
    """Check an exact already-selected model without substituting a cheaper engine."""
    decision = resolve_lane_model(
        requested_engine=model,
        required_model=model,
        lane_kind=lane_kind,
        code_edit_ready=True,
        escalation_reason=escalation_reason,
    )
    return None if decision.allowed else decision.reason
