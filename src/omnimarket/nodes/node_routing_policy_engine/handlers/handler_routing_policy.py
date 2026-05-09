# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Routing policy handler — pure, deterministic model selection.

Selects the optimal model for a task via exploitation (best eligible model) or
exploration (second-best eligible model) based on a caller-supplied seed value.
No I/O, no randomness, no side effects.
"""

from __future__ import annotations

from omnimarket.nodes.node_routing_policy_engine.models.model_routing_policy_request import (
    ModelAvailableModel,
    ModelRoutingPolicyRequest,
)
from omnimarket.nodes.node_routing_policy_engine.models.model_routing_policy_result import (
    EnumRoutingStatus,
    EnumSelectionMode,
    ModelRankedCandidate,
    ModelRoutingPolicyResult,
)


def _is_eligible(
    model: ModelAvailableModel,
    request: ModelRoutingPolicyRequest,
) -> bool:
    if (
        request.max_cost_per_token is not None
        and model.cost_per_token > request.max_cost_per_token
    ):
        return False
    if not request.required_capabilities.issubset(model.capabilities):
        return False
    return True


def _rank_candidates(
    models: tuple[ModelAvailableModel, ...],
    request: ModelRoutingPolicyRequest,
) -> list[ModelAvailableModel]:
    eligible = [m for m in models if _is_eligible(m, request)]
    return sorted(eligible, key=lambda m: m.score, reverse=True)


class HandlerRoutingPolicy:
    """Select the optimal model key given constraints and an exploit/explore policy."""

    def handle(self, request: ModelRoutingPolicyRequest) -> ModelRoutingPolicyResult:
        ranked = _rank_candidates(request.available_models, request)

        if not ranked:
            return ModelRoutingPolicyResult(
                status=EnumRoutingStatus.ERROR,
                request_id=request.request_id,
                error="No eligible models after applying cost and capability constraints.",
            )

        is_explore = (
            len(ranked) >= 2 and request.exploration_seed < request.exploration_rate
        )

        if is_explore:
            selected = ranked[1]
            mode = EnumSelectionMode.EXPLORE
            reason = (
                f"Exploration: seed {request.exploration_seed:.4f} < "
                f"rate {request.exploration_rate:.4f}; "
                f"selected second-best model '{selected.key}'."
            )
            others = [ranked[0], *ranked[2:]]
        else:
            selected = ranked[0]
            mode = EnumSelectionMode.EXPLOIT
            reason = (
                f"Exploitation: selected best-scoring eligible model '{selected.key}' "
                f"with score {selected.score:.4f}."
            )
            others = ranked[1:]

        alternatives = tuple(
            ModelRankedCandidate(
                key=m.key,
                score=m.score,
                cost_per_token=m.cost_per_token,
                rank=idx + 1,
            )
            for idx, m in enumerate(others)
        )

        return ModelRoutingPolicyResult(
            status=EnumRoutingStatus.OK,
            selected_model_key=selected.key,
            selection_mode=mode,
            selection_reason=reason,
            alternative_candidates=alternatives,
            request_id=request.request_id,
        )


__all__ = ["HandlerRoutingPolicy"]
