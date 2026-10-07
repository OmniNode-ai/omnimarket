# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one result of the acceptance judge; only the fields of the operation are filled."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.models.delegation_acceptance_judge.enum_acceptance_operation import (
    EnumAcceptanceOperation,
)
from omnimarket.models.delegation_acceptance_judge.model_acceptance_verdict import (
    ModelAcceptanceVerdict,
)
from omnimarket.nodes.node_delegation_acceptance_judge_compute.models.enum_acceptance_run_status import (
    EnumAcceptanceRunStatus,
)
from omnimarket.nodes.node_delegation_acceptance_judge_compute.models.model_acceptance_agreement import (
    ModelAcceptanceAgreement,
)
from omnimarket.nodes.node_delegation_acceptance_judge_compute.models.model_acceptance_batch import (
    ModelAcceptanceBatch,
)
from omnimarket.nodes.node_delegation_acceptance_judge_compute.models.model_acceptance_issue import (
    ModelAcceptanceIssue,
)
from omnimarket.nodes.node_delegation_acceptance_judge_compute.models.model_acceptance_matrix_row import (
    ModelAcceptanceMatrixRow,
)


class ModelAcceptanceJudgeResult(BaseModel):
    """The one result of the acceptance judge; only the fields of the operation are filled."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: EnumAcceptanceOperation
    rubric_version: str
    status: EnumAcceptanceRunStatus
    issues: tuple[ModelAcceptanceIssue, ...] = ()
    batches: tuple[ModelAcceptanceBatch, ...] = ()
    excluded_probe_ids: tuple[str, ...] = ()
    dropped_item_ids: tuple[str, ...] = ()
    double_judge_item_ids: tuple[str, ...] = ()
    verdicts: tuple[ModelAcceptanceVerdict, ...] = ()
    agreement: ModelAcceptanceAgreement | None = None
    matrix: tuple[ModelAcceptanceMatrixRow, ...] = ()
    events_counted: int = 0
    volume_weighted_accept_rate: float | None = None
    volume_terminal_ok_rate: float | None = None
    receipts_joined: int = 0
    report_markdown: str = ""
