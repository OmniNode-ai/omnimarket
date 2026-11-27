# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input and output models of the help step (OMN-26722)."""

from __future__ import annotations

from omnibase_core.models.lane_work import ModelLaneHelpRequest, ModelLaneWorkReceipt
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.nodes.node_lane_work_orchestrator.models.model_lane_help_receipt import (
    ModelLaneHelpReceipt,
)
from omnimarket.nodes.node_lane_work_reducer.models.model_lane_work_fold_input import (
    ModelLaneWorkFoldInput,
)
from omnimarket.nodes.node_lane_work_reducer.models.model_lane_work_intent import (
    ModelLaneWorkIntent,
)
from omnimarket.nodes.node_lane_work_reducer.models.model_lane_work_reducer_state import (
    ModelLaneWorkReducerState,
)


class ModelLaneWorkHelpBuildRequest(BaseModel):
    """The state after a `help_request` intent, that intent, and what the question carries."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: ModelLaneWorkReducerState
    intent: ModelLaneWorkIntent
    question: str = Field(min_length=1)
    last_receipts: tuple[ModelLaneWorkReceipt, ...] = Field(default=(), max_length=3)
    evidence_paths: tuple[str, ...] = ()
    five_whys: tuple[str, ...] = ()


class ModelLaneWorkHelpDispatch(BaseModel):
    """One pinned request, or the `help_failed` fold input when no tier is above the worker."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    help_request: ModelLaneHelpRequest
    delegate_request: ModelDelegateSkillRequest | None = None
    ledger_message: str = Field(min_length=1)
    fold_event: ModelLaneWorkFoldInput | None = None
    receipt: ModelLaneHelpReceipt | None = None


class ModelLaneWorkHelpAnswerInput(BaseModel):
    """A pinned delegation's completed (or failed) result, with the attempt record's backend and model."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: ModelLaneWorkReducerState
    correlation_id: str = Field(min_length=1)
    failed: bool = False
    answer_text: str = ""
    executed_backend_id: str | None = None
    executed_model: str | None = None


class ModelLaneWorkHelpAnswerResult(BaseModel):
    """The typed event for the fold (None when the answer is refused) and the receipt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    fold_event: ModelLaneWorkFoldInput | None = None
    receipt: ModelLaneHelpReceipt


__all__ = [
    "ModelLaneWorkHelpAnswerInput",
    "ModelLaneWorkHelpAnswerResult",
    "ModelLaneWorkHelpBuildRequest",
    "ModelLaneWorkHelpDispatch",
]
