# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one request of the acceptance judge: an operation and the inputs it needs."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.nodes.node_delegation_acceptance_judge_compute.models.enum_acceptance_operation import (
    EnumAcceptanceOperation,
)
from omnimarket.nodes.node_delegation_acceptance_judge_compute.models.model_acceptance_cell_event import (
    ModelAcceptanceCellEvent,
)
from omnimarket.nodes.node_delegation_acceptance_judge_compute.models.model_acceptance_item import (
    ModelAcceptanceItem,
)
from omnimarket.nodes.node_delegation_acceptance_judge_compute.models.model_acceptance_receipt_join import (
    ModelAcceptanceReceiptJoin,
)
from omnimarket.nodes.node_delegation_acceptance_judge_compute.models.model_acceptance_verdict import (
    ModelAcceptanceVerdict,
)


class ModelAcceptanceJudgeRequest(BaseModel):
    """The one request of the acceptance judge: an operation and the inputs it needs.

    render:  ``items``, ``seed`` and an optional ``cell_cap``; returns the primary and second-judge batches.
    check:   ``batch_item_ids`` and ``reply_text``; returns the validated verdicts or the issues.
    score:   ``items``, ``primary_verdicts``, ``secondary_verdicts``, ``cell_events`` and ``receipts``.
    ``rubric_yaml`` is the text of delegation_acceptance_judge_rubric.v1.yaml: the caller reads the
    file and passes its content, because the node does no I/O.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: EnumAcceptanceOperation
    rubric_yaml: str = Field(min_length=1)
    seed: str = ""
    cell_cap: int = Field(default=0, ge=0)
    items: tuple[ModelAcceptanceItem, ...] = ()
    batch_item_ids: tuple[str, ...] = ()
    reply_text: str = ""
    primary_verdicts: tuple[ModelAcceptanceVerdict, ...] = ()
    secondary_verdicts: tuple[ModelAcceptanceVerdict, ...] = ()
    cell_events: tuple[ModelAcceptanceCellEvent, ...] = ()
    receipts: tuple[ModelAcceptanceReceiptJoin, ...] = ()

    @model_validator(mode="after")
    def _operation_has_its_inputs(self) -> ModelAcceptanceJudgeRequest:
        if self.operation is EnumAcceptanceOperation.RENDER:
            if not self.items:
                raise ValueError("render needs items")
            if not self.seed:
                raise ValueError("render needs a seed so the sample is reproducible")
        elif self.operation is EnumAcceptanceOperation.CHECK:
            if not self.batch_item_ids:
                raise ValueError("check needs the batch item ids")
        elif not self.items or not self.primary_verdicts:
            raise ValueError("score needs items and primary verdicts")
        return self
