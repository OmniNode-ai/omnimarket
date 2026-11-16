# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ladder on a rubric failure, as a pure decision (OMN-20172, plan 13.6 and 13.10).

Verified against ``formal/delegation_shrink_retry``: a whole that failed the
class rubric first takes the one configuration swap when a second MET
configuration exists, then shrinks when the class is decomposable and the
depth cap allows, and only then escalates. A failed unit splits once more or
escalates alone. A recombined whole that fails its rubric escalates. The caps
come from the class contract and are never defaulted here.
"""

from __future__ import annotations

from collections.abc import Iterable

from omnimarket.models.delegation.model_shrink_retry import (
    EnumShrinkRetryReason,
    EnumShrinkRetryRung,
    EnumShrinkRetryScope,
    ModelShrinkRetryDecision,
    ModelShrinkRetryDecisionRequest,
)
from omnimarket.models.delegation.model_size_band import EnumSizeBand
from omnimarket.models.delegation.wire.model_attempt_configuration import (
    ModelAttemptConfiguration,
)
from omnimarket.models.delegation_capability_map.enum_capability_cell_verdict import (
    EnumCapabilityCellVerdict,
)
from omnimarket.models.delegation_capability_map.model_capability_cell import (
    ModelCapabilityCell,
)


def next_best_configuration(
    cells: Iterable[ModelCapabilityCell],
    *,
    model_key: str,
    backend_id: str,
    task_class: str,
    size_band: EnumSizeBand,
    current: ModelAttemptConfiguration,
) -> ModelAttemptConfiguration | None:
    """The MET cell with the highest lower bound whose configuration is not ``current``.

    A deterministic read of stored cells, as plan 13.10 requires: nothing is
    searched on a live request. Ties break on configuration id. None when the
    model has no second MET configuration for the class and band.
    """
    met = sorted(
        (
            cell
            for cell in cells
            if cell.verdict is EnumCapabilityCellVerdict.MET
            and cell.model_key == model_key
            and cell.backend_id == backend_id
            and cell.task_class == task_class
            and cell.size_band is size_band
            and cell.configuration_parameters != current
        ),
        key=lambda cell: (-cell.decision_lower_bound, cell.configuration_id),
    )
    return met[0].configuration_parameters if met else None


class HandlerShrinkRetryPolicy:
    """Stateless; the request carries the ladder's whole state."""

    def handle(
        self, request: ModelShrinkRetryDecisionRequest
    ) -> ModelShrinkRetryDecision:
        def decide(
            rung: EnumShrinkRetryRung,
            reason: EnumShrinkRetryReason,
            configuration: ModelAttemptConfiguration | None = None,
        ) -> ModelShrinkRetryDecision:
            return ModelShrinkRetryDecision(
                task_id=request.task_id,
                rung=rung,
                reason=reason,
                configuration=configuration,
            )

        if request.scope is EnumShrinkRetryScope.RECOMBINED:
            return decide(
                EnumShrinkRetryRung.ESCALATE_WHOLE,
                EnumShrinkRetryReason.WHOLE_CHECK_FAILED,
            )
        caps = request.caps
        if request.scope is EnumShrinkRetryScope.UNIT:
            if caps is None:
                return decide(
                    EnumShrinkRetryRung.ESCALATE_UNIT,
                    EnumShrinkRetryReason.SHRINK_NOT_DECLARED,
                )
            if not request.splittable:
                return decide(
                    EnumShrinkRetryRung.ESCALATE_UNIT,
                    EnumShrinkRetryReason.NOT_DECOMPOSABLE,
                )
            if request.split_depth >= caps.max_split_depth:
                return decide(
                    EnumShrinkRetryRung.ESCALATE_UNIT,
                    EnumShrinkRetryReason.SPLIT_DEPTH_CAP,
                )
            # A split yields at least two units, so fewer than two attempts of room
            # cannot take one.
            if (
                request.unit_attempts_used + 2
                > caps.unit_attempts_per_unit * request.unit_count
            ):
                return decide(
                    EnumShrinkRetryRung.ESCALATE_UNIT,
                    EnumShrinkRetryReason.UNIT_ATTEMPT_CAP,
                )
            return decide(
                EnumShrinkRetryRung.RESPLIT_UNIT, EnumShrinkRetryReason.DECOMPOSABLE
            )
        # The whole request failed the class rubric.
        if caps is None:
            return decide(
                EnumShrinkRetryRung.ESCALATE_WHOLE,
                EnumShrinkRetryReason.SHRINK_NOT_DECLARED,
            )
        if (
            request.second_configuration is not None
            and request.config_swaps_used < caps.max_config_swaps
        ):
            return decide(
                EnumShrinkRetryRung.CONFIG_SWAP,
                EnumShrinkRetryReason.SECOND_MET_CONFIGURATION,
                request.second_configuration,
            )
        if not request.splittable:
            return decide(
                EnumShrinkRetryRung.ESCALATE_WHOLE,
                EnumShrinkRetryReason.NOT_DECOMPOSABLE,
            )
        if request.split_depth >= caps.max_split_depth:
            return decide(
                EnumShrinkRetryRung.ESCALATE_WHOLE,
                EnumShrinkRetryReason.SPLIT_DEPTH_CAP,
            )
        return decide(EnumShrinkRetryRung.SHRINK, EnumShrinkRetryReason.DECOMPOSABLE)
