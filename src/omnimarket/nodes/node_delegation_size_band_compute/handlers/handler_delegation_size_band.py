# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Definition-B size measurement over a composition-owned authority snapshot."""

from omnimarket.handlers.handler_size_band_measurement import measure_size_band
from omnimarket.inference.task_class_authority import (
    ModelTaskClassAuthority,
    load_task_class_authority,
)
from omnimarket.models.delegation.model_size_band import (
    EnumSizeInput,
    ModelSizeBand,
    ModelSizeBandRefusal,
    ModelSizeBandRequest,
)


class HandlerDelegationSizeBand:
    """Stateless compute that refuses declared sizes before measuring text."""

    def __init__(self, authority: ModelTaskClassAuthority | None = None) -> None:
        self._authority = (
            load_task_class_authority() if authority is None else authority
        )

    def handle(
        self, request: ModelSizeBandRequest
    ) -> ModelSizeBand | ModelSizeBandRefusal:
        """Measure trusted text inputs without reading files or mutating state."""
        supplied = tuple(
            field.value
            for field in EnumSizeInput
            if field.value in request.model_fields_set
        )
        if supplied:
            return ModelSizeBandRefusal(reason="size_input_supplied", fields=supplied)
        if request.task_class not in self._authority.task_classes:
            return ModelSizeBandRefusal(
                reason="task_class_unavailable", fields=("task_class",)
            )
        resolved = self._authority.size_band_thresholds_for(request.task_class)
        if resolved is None:
            return ModelSizeBandRefusal(
                reason="size_thresholds_unavailable", fields=("size_band_thresholds",)
            )
        thresholds, reference = resolved
        return measure_size_band(
            task_class=request.task_class,
            prompt=request.prompt,
            context_pack=request.context_pack,
            sources=request.sources,
            acceptance_criteria=request.acceptance_criteria,
            thresholds=thresholds,
            thresholds_reference=reference,
        )
