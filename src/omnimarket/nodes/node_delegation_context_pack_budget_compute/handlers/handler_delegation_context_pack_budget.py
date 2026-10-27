# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Decide delegation context-pack budget and admission (OMN-21119, plan TA.18 AC1/AC3)."""

from omnimarket.nodes.node_delegation_context_pack_budget_compute.models.model_delegation_context_pack_budget import (
    EnumDelegationContextPackStatus,
    ModelDelegationContextPackBudgetRequest,
    ModelDelegationContextPackBudgetResult,
)


class HandlerDelegationContextPackBudget:
    """Deterministic compute over profile budgets, backend token counts and pack answers."""

    def handle(
        self, request: ModelDelegationContextPackBudgetRequest
    ) -> ModelDelegationContextPackBudgetResult:
        """Bound the pack budget and decide whether dispatch can proceed."""
        token_budget_override: int | None = None
        context_pack_hash = ""
        if request.context_profile is None:
            status = EnumDelegationContextPackStatus.NOT_REQUESTED
            dispatch_allowed = True
            reason = f"task class {request.task_class} names no context profile"
        else:
            room = request.max_grounded_input_tokens - request.composed_request_tokens
            if room <= 0:
                status = EnumDelegationContextPackStatus.NO_ROOM
                dispatch_allowed = True
                reason = (
                    "no room under the grounded-input ceiling; dispatch without a pack"
                )
            else:
                # Request validation pairs every profile with a positive budget.
                assert request.profile_token_budget is not None
                token_budget_override = min(room, request.profile_token_budget)
                if request.context_pack_hash is None:
                    status = EnumDelegationContextPackStatus.REQUEST_PACK
                    dispatch_allowed = False
                    reason = "request a context pack within the remaining grounded-input budget"
                elif request.context_pack_hash.strip() == "":
                    status = EnumDelegationContextPackStatus.REFUSE_EMPTY_HASH
                    dispatch_allowed = False
                    reason = (
                        f"context pack for task class {request.task_class} answered "
                        f"with an empty hash; correlation_id {request.correlation_id}"
                    )
                elif request.assembled_request_tokens is None:
                    status = EnumDelegationContextPackStatus.REFUSE_UNMEASURED_INPUT
                    dispatch_allowed = False
                    reason = (
                        "full serialized input including the pack has not been counted"
                    )
                elif (
                    request.assembled_request_tokens > request.max_grounded_input_tokens
                ):
                    status = EnumDelegationContextPackStatus.REFUSE_INPUT_OVERFLOW
                    dispatch_allowed = False
                    reason = "full serialized input including the pack exceeds the grounded-input ceiling"
                else:
                    status = EnumDelegationContextPackStatus.ATTACH
                    dispatch_allowed = True
                    context_pack_hash = request.context_pack_hash.strip()
                    reason = "context pack has a non-empty hash and measured input fits; attach and dispatch"

        return ModelDelegationContextPackBudgetResult(
            correlation_id=request.correlation_id,
            task_class=request.task_class,
            context_profile=request.context_profile,
            status=status,
            dispatch_allowed=dispatch_allowed,
            token_budget_override=token_budget_override,
            context_pack_hash=context_pack_hash,
            composed_request_tokens=request.composed_request_tokens,
            max_grounded_input_tokens=request.max_grounded_input_tokens,
            token_estimator=request.token_estimator,
            assembled_request_tokens=request.assembled_request_tokens,
            reason=reason,
        )
