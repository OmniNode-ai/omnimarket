# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input and output of the code eval trial reducer (OMN-23627).

Exemplar: ``nodes/node_pr_landing_reducer/models`` (the stored row plus one
message to the next row plus emissions). A message is one of five kinds,
discriminated by ``kind``. Time and the free hosts travel in the input: the
reducer reads no clock and no capacity.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.code_eval_trial.model_code_eval_run_closed import (
    ModelCodeEvalRunClosed,
)
from omnimarket.models.code_eval_trial.model_code_eval_run_nudge import (
    ModelCodeEvalRunNudge,
)
from omnimarket.models.code_eval_trial.model_code_eval_run_request import (
    ModelCodeEvalRunRequest,
)
from omnimarket.models.code_eval_trial.model_code_eval_run_row import (
    ModelCodeEvalRunRow,
    ModelCodeEvalSettings,
)
from omnimarket.models.enum_code_score import (
    EnumCodeScoreSource,
    EnumCodeScoreVerdict,
)


class ModelResolvedTask(BaseModel):
    """One candidate after the run leg read it: admitted with its band, or excluded with a reason."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_key: str = Field(min_length=1)
    band: str = ""
    excluded_reason: str | None = None


class ModelRunRequestedMessage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["run_requested"] = "run_requested"
    request: ModelCodeEvalRunRequest
    tasks: tuple[ModelResolvedTask, ...]
    settings: ModelCodeEvalSettings
    digest: str


class ModelUnitAnsweredMessage(BaseModel):
    """The work unit's receipt, reduced to what rule 2's table reads."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["unit_answered"] = "unit_answered"
    unit_id: str
    status: Literal["completed", "timed_out", "refused", "infra_error"]
    exit_code: int | None = None
    diff: str = ""
    diff_truncated: bool = False
    #: The loop result's status; None when the receipt carried no code_edit_result.
    loop_status: str | None = None
    duration_s: float = Field(default=0.0, ge=0)


class ModelUnitFailedMessage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["unit_failed"] = "unit_failed"
    unit_id: str


class ModelScoreAnsweredMessage(BaseModel):
    """E2's scored event, matched by the try identity (task, model arm, ordinal)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["score_answered"] = "score_answered"
    task_key: str
    model_arm: str = ""
    try_ordinal: int = Field(default=1, ge=1)
    verdict: EnumCodeScoreVerdict
    source: EnumCodeScoreSource | None = None


class ModelNudgeMessage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["nudge"] = "nudge"
    nudge: ModelCodeEvalRunNudge


ModelCodeEvalMessage = Annotated[
    ModelRunRequestedMessage
    | ModelUnitAnsweredMessage
    | ModelUnitFailedMessage
    | ModelScoreAnsweredMessage
    | ModelNudgeMessage,
    Field(discriminator="kind"),
]


class ModelCodeEvalReduceInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    row: ModelCodeEvalRunRow | None = None
    message: ModelCodeEvalMessage
    now: datetime
    free_hosts: tuple[str, ...] = ()


class ModelScoreToSend(BaseModel):
    """A score request to send; an empty model_arm is a task's control."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_key: str
    model_arm: str = ""
    context_arm: Literal["off", "held"]
    try_ordinal: int
    draft_diff: str = ""
    timed_out_with_diff: bool = False


class ModelUnitToPlace(BaseModel):
    """A try placed on a free host; the orchestrator sends the work unit."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    unit_id: str
    task_key: str
    model: str
    backend_id: str
    try_ordinal: int
    host: str


class ModelVerdictRow(BaseModel):
    """One verdict to publish as its context-ROI row."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_key: str
    model_arm: str = ""
    try_ordinal: int
    is_control: bool
    verdict: EnumCodeScoreVerdict
    source: EnumCodeScoreSource | None = None
    band: str = ""
    loop_status: str = ""
    duration_s: float = 0.0
    timed_out_with_diff: bool = False


class ModelCodeEvalReduceOutput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    row: ModelCodeEvalRunRow | None
    #: The transition taken, or the reason the message was dropped (the row is then unchanged).
    transition: str
    dropped: bool = False
    score_requests: tuple[ModelScoreToSend, ...] = ()
    unit_requests: tuple[ModelUnitToPlace, ...] = ()
    verdict_rows: tuple[ModelVerdictRow, ...] = ()
    closed: ModelCodeEvalRunClosed | None = None
    #: True when the row differs from the stored one (so a write is needed).
    row_changed: bool = False


__all__ = [
    "ModelCodeEvalMessage",
    "ModelCodeEvalReduceInput",
    "ModelCodeEvalReduceOutput",
    "ModelNudgeMessage",
    "ModelResolvedTask",
    "ModelRunRequestedMessage",
    "ModelScoreAnsweredMessage",
    "ModelScoreToSend",
    "ModelUnitAnsweredMessage",
    "ModelUnitFailedMessage",
    "ModelUnitToPlace",
    "ModelVerdictRow",
]
