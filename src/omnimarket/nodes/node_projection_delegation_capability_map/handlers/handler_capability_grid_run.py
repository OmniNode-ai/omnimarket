# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The offline configuration grid over the labelled sets (OMN-20169 AC6, plan 13.10).

Runs every model under every configuration (at most eight) over every labelled
item once, through an injected grader that answers and checks one item, and
returns one verdict row per (item, model, configuration). It is run offline in
batches by a lab command; nothing on the routing path imports it, so no
configuration is chosen, tuned or searched on a live request.

A grader that returns a ``ModelCapabilityGridGrade`` names the machine that
served the answer (OMN-21434). The row then records the host and the answering
backend, and its identity carries the backend that answered and the model the
endpoint reported, so an answer served by another backend or model is refused
by the fold, and one with no answering backend lacks an identity field.
"""

from __future__ import annotations

from typing import Protocol

from omnimarket.models.delegation_capability_map.enum_capability_source import (
    EnumCapabilitySource,
)
from omnimarket.models.delegation_capability_map.model_capability_configuration import (
    ModelCapabilityConfiguration,
)
from omnimarket.models.delegation_capability_map.model_capability_grid import (
    ModelCapabilityGridGrade,
    ModelCapabilityGridItem,
    ModelCapabilityGridModel,
    ModelCapabilityGridRequest,
)
from omnimarket.models.delegation_capability_map.model_capability_verdict_row import (
    ModelCapabilityVerdictRow,
)


class ProtocolCapabilityGridGrader(Protocol):
    """Answers one item on one model under one configuration and checks the answer.

    Returns True when the class rubric passes the answer (or its label says
    adequate), or a grade that also names the machine that served the answer.
    """

    def __call__(
        self,
        item: ModelCapabilityGridItem,
        model: ModelCapabilityGridModel,
        configuration: ModelCapabilityConfiguration,
    ) -> bool | ModelCapabilityGridGrade: ...


class HandlerCapabilityGridRun:
    """One verdict row per item, model and configuration, in a fixed order."""

    def __init__(self, grader: ProtocolCapabilityGridGrader) -> None:
        self._grader = grader

    def handle(
        self, request: ModelCapabilityGridRequest
    ) -> tuple[ModelCapabilityVerdictRow, ...]:
        rows: list[ModelCapabilityVerdictRow] = []
        for model in request.models:
            for configuration in request.configurations:
                identity = request.identity_for(model, configuration)
                for item in request.items:
                    graded = self._grader(item, model, configuration)
                    grade = (
                        graded if isinstance(graded, ModelCapabilityGridGrade) else None
                    )
                    rows.append(
                        ModelCapabilityVerdictRow(
                            tenant_id=request.tenant_id,
                            item_key=item.item_key,
                            source=EnumCapabilitySource.GRID_RUN,
                            task_class=item.task_class,
                            size_band=item.size_band,
                            model_key=model.model_key,
                            backend_id=model.backend_id,
                            configuration=configuration,
                            identity=(
                                identity
                                if grade is None
                                else identity.model_copy(
                                    update={
                                        "backend": grade.answering_backend,
                                        "model_revision": grade.served_model,
                                    }
                                )
                            ),
                            passed=grade.passed if grade is not None else bool(graded),
                            host=grade.host if grade is not None else None,
                            answering_backend=(
                                grade.answering_backend if grade is not None else None
                            ),
                        )
                    )
        return tuple(rows)
