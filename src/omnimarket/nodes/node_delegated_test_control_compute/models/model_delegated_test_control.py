# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input and output of the must-fail control grading compute (OMN-19361).

Outcomes arrive as the pytest failure digest's outcome strings (``passed``,
``failed_call``, ``failed_collection``, ``error_setup``, ``no_tests``,
``infra_error``). They are plain strings here on purpose: this node does not
import another node's models; the orchestrator maps one onto the other.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

RunOutcome = Literal[
    "passed",
    "failed_call",
    "failed_collection",
    "error_setup",
    "no_tests",
    "infra_error",
]


class EnumControlStatus(StrEnum):
    """The control's verdict. Only the two headline statuses are headline-grade."""

    ACCEPTED_CALL = "accepted_call"
    ACCEPTED_MUTATION = "accepted_mutation"
    ACCEPTED_COLLECTION = "accepted_collection"
    CONTROL_DID_NOT_FAIL = "control_did_not_fail"
    NEEDS_MUTATION_CONTROL = "needs_mutation_control"
    INFRA_ERROR = "infra_error"


#: Operator ruling 2026-09-23T21:52:08Z decision (3): the headline needs an
#: assertion-level failure of the control.
HEADLINE_STATUSES: frozenset[EnumControlStatus] = frozenset(
    {EnumControlStatus.ACCEPTED_CALL, EnumControlStatus.ACCEPTED_MUTATION}
)


class ModelControlGradeRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    fixed_outcome: RunOutcome = Field(..., description="The run at the fixed ref.")
    prefix_outcome: RunOutcome = Field(
        ..., description="The same test at the pre-fix ref."
    )
    mutation_outcome: RunOutcome | None = Field(
        default=None, description="The same test at the mutated fixed ref, if it ran."
    )
    mutation_requested: bool = Field(
        ..., description="The loop request carries a mutation control."
    )
    prefix_ref_equals_fixed_ref: bool = Field(
        ..., description="The negative control: the pre-fix ref is the fixed ref."
    )

    @model_validator(mode="after")
    def _control_follows_a_pass(self) -> ModelControlGradeRequest:
        if self.fixed_outcome != "passed":
            raise ValueError(
                "a control is graded only after the test passed at the fixed ref"
            )
        return self


class ModelControlGrade(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: EnumControlStatus
    headline: bool
    control_ref_role: Literal["prefix", "mutation"]
    control_outcome: str
    reason: str


__all__ = [
    "HEADLINE_STATUSES",
    "EnumControlStatus",
    "ModelControlGrade",
    "ModelControlGradeRequest",
    "RunOutcome",
]
