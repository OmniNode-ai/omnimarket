# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The projection's request: the clock reading, the rules, the scope and every source's rows."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from omnimarket.models.model_reading import ModelLedgerEvidenceRow
from omnimarket.models.work_state_projection.model_work_state_rules import (
    ModelWorkScope,
    ModelWorkStateRules,
)
from omnimarket.models.work_state_projection.model_work_state_sources import (
    ModelLaneReceiptSource,
    ModelPlanTaskSource,
    ModelPullRequestSource,
    ModelReadingSource,
    ModelTicketSource,
)


class ModelWorkStateRequest(BaseModel):
    """Everything one projection run reads. A source that could not be read is named in `unread`, never left empty."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    now: datetime
    rules: ModelWorkStateRules
    scope: ModelWorkScope
    tickets: tuple[ModelTicketSource, ...] = ()
    plan_tasks: tuple[ModelPlanTaskSource, ...] = ()
    pull_requests: tuple[ModelPullRequestSource, ...] = ()
    ledger_rows: tuple[ModelLedgerEvidenceRow, ...] = ()
    receipts: tuple[ModelLaneReceiptSource, ...] = ()
    readings: tuple[ModelReadingSource, ...] = ()
    unread: tuple[str, ...] = ()


__all__ = ["ModelWorkStateRequest"]
