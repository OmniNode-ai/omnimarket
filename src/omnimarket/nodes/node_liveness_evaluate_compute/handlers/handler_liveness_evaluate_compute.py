# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerLivenessEvaluateCompute — pure demand-aware liveness state decision.

OMN-15126 implementation of the OMN-14845 design (design §3.2 ordered
evaluation pipeline, adopted per design OPEN-1's recommendation):

    1. registry resolution failure                -> NOT_READY (terminal)
    2. demand-source query failure                 -> NOT_READY (terminal)
    3. eligible demand exists this cycle:
         failed_ratio > error_budget_ratio         -> RED (terminal)
         failed_ratio <= error_budget_ratio        -> HEALTHY (terminal)
    4. zero eligible demand this cycle:
         fresh prior HEALTHY on record              -> NO_DEMAND
         no prior HEALTHY, or it is stale           -> STALE

Steps 1-2 are I/O outcomes computed upstream by
node_liveness_demand_query_effect (or an orchestrator's own registry
lookup); this handler performs ONLY the state decision -- no I/O, no
network, no clock (the caller supplies `evaluated_at`), fully deterministic
given its input. Every returned receipt is constructed via
`ModelLivenessReceipt` (omnibase_core), whose own per-state
`model_validator` is the second, independent enforcement of the design's
required/forbidden field rules (design §5).
"""

from __future__ import annotations

import logging
from typing import Any, Literal
from uuid import uuid4

from omnibase_core.enums.enum_liveness_state import EnumLivenessState
from omnibase_core.models.runtime.model_liveness_receipt import ModelLivenessReceipt

from omnimarket.nodes.node_liveness_evaluate_compute.models.model_liveness_evaluate_request import (
    ModelLivenessEvaluateRequest,
)

logger = logging.getLogger(__name__)


def _base_kwargs(request: ModelLivenessEvaluateRequest) -> dict[str, Any]:
    return {
        "receipt_id": uuid4(),
        "surface_id": request.surface_id,
        "lane": request.lane,
        "deployed_sha": request.deployed_sha,
        "image_digest": request.image_digest,
        "config_digest": request.config_digest,
        "evaluated_at": request.evaluated_at,
        "freshness_window_seconds": request.freshness_window_seconds,
        "runner": request.runner,
        "independent_verifier": request.independent_verifier,
        "demand_synthetic": request.demand_synthetic,
    }


def _evaluate(request: ModelLivenessEvaluateRequest) -> ModelLivenessReceipt:
    """Pure state decision (design §3.2). Deterministic given `request`."""
    base = _base_kwargs(request)

    # Step 1: registry resolution failure -> NOT_READY (terminal).
    if not request.registry_resolved:
        return ModelLivenessReceipt(
            **base,
            state=EnumLivenessState.NOT_READY,
            not_ready_reason=request.not_ready_reason or "registry entry unresolved",
        )

    # Step 2: demand-source query failure -> NOT_READY (terminal).
    if not request.demand_query_succeeded:
        return ModelLivenessReceipt(
            **base,
            state=EnumLivenessState.NOT_READY,
            not_ready_reason=request.not_ready_reason or "demand source query failed",
        )

    # Step 3: eligible demand exists this cycle -> HEALTHY or RED.
    if request.eligible_count > 0:
        checked = request.checked_count
        failed = request.failed_count
        failed_ratio = (failed / checked) if checked else 1.0

        if failed_ratio > request.error_budget_ratio:
            return ModelLivenessReceipt(
                **base,
                state=EnumLivenessState.RED,
                correlation_id=request.correlation_id,
                input_event_ref=request.input_event_ref,
                terminal_event_ref=request.terminal_event_ref,
                projection_key_canonical=request.projection_key_canonical,
                projection_value_hash=request.projection_value_hash,
                projection_expected_value_hash=request.projection_expected_value_hash,
                expected_value_predicate_result=request.expected_value_predicate_result,
                checked_count=checked,
                failed_count=failed,
                failed_ratio=failed_ratio,
                sampling_applied=request.sampling_applied,
                failure_detail=request.failure_detail
                or (
                    f"failed_ratio={failed_ratio:.4f} exceeds "
                    f"error_budget_ratio={request.error_budget_ratio:.4f} "
                    f"({failed}/{checked} eligible correlations failed the join)"
                ),
            )

        return ModelLivenessReceipt(
            **base,
            state=EnumLivenessState.HEALTHY,
            correlation_id=request.correlation_id,
            input_event_ref=request.input_event_ref,
            terminal_event_ref=request.terminal_event_ref,
            projection_key_canonical=request.projection_key_canonical,
            projection_value_hash=request.projection_value_hash,
            projection_expected_value_hash=request.projection_expected_value_hash,
            expected_value_predicate_result=request.expected_value_predicate_result,
            checked_count=checked,
            failed_count=failed,
            failed_ratio=failed_ratio,
            sampling_applied=request.sampling_applied,
        )

    # Step 4: zero eligible demand this cycle -> NO_DEMAND or STALE.
    if request.prior_healthy_at is not None:
        age_seconds = (request.evaluated_at - request.prior_healthy_at).total_seconds()
        if age_seconds <= request.freshness_window_seconds:
            return ModelLivenessReceipt(
                **base,
                state=EnumLivenessState.NO_DEMAND,
                demand_query_evidence=request.demand_query_evidence
                or "eligible_count=0",
            )

    return ModelLivenessReceipt(
        **base,
        state=EnumLivenessState.STALE,
        last_healthy_receipt_id=request.prior_healthy_receipt_id,
        last_healthy_at=request.prior_healthy_at,
    )


class HandlerLivenessEvaluateCompute:
    """COMPUTE handler: pure demand-aware liveness state decision (design §3.2)."""

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["COMPUTE"]:
        return "COMPUTE"

    async def handle(
        self,
        request: ModelLivenessEvaluateRequest,
    ) -> ModelLivenessReceipt:
        receipt = _evaluate(request)
        logger.info(
            "liveness_evaluate_compute: surface_id=%s state=%s correlation_id=%s",
            request.surface_id,
            receipt.state.value,
            receipt.correlation_id,
        )
        return receipt


__all__ = ["HandlerLivenessEvaluateCompute"]
