# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Output of node_pr_handoff_decision_compute (OMN-20636)."""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.pr_handoff.enum_pr_handoff_error_code import (
    DECISION_REFUSAL_CODES,
    EnumPrHandoffErrorCode,
)
from omnimarket.models.pr_handoff.enum_pr_handoff_verdict import (
    EnumPrHandoffVerdict,
)
from omnimarket.models.pr_handoff.enum_pr_handoff_wait_reason import (
    EnumPrHandoffWaitReason,
)


class ModelPrHandoffDecision(BaseModel):
    """Ready (with the rows), wait (with the reason) or refuse (with the code).

    ``rows`` are the handoff MSG followed by the lane's closing row (TERMINAL,
    or STATUS in session mode, or nothing with msg_only), byte for byte what
    pr-handoff's handoff_row.sh prints, stamped with the evaluating time.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: EnumPrHandoffVerdict
    wait_reason: EnumPrHandoffWaitReason | None = None
    error_code: EnumPrHandoffErrorCode | None = None
    detail: str = Field(default="", max_length=2000)
    live_head_sha: str | None = None
    ticket: str | None = None
    ticket_source: str | None = None
    msg_id: str | None = None
    rows: str | None = None

    @model_validator(mode="after")
    def _verdict_shape(self) -> Self:
        ready = self.verdict is EnumPrHandoffVerdict.READY
        wait = self.verdict is EnumPrHandoffVerdict.WAIT
        refuse = self.verdict is EnumPrHandoffVerdict.REFUSE
        if ready != (self.rows is not None and self.msg_id is not None):
            raise ValueError(
                "rows and msg_id are set exactly when the verdict is ready"
            )
        if wait != (self.wait_reason is not None):
            raise ValueError("wait_reason is set exactly when the verdict is wait")
        if refuse != (self.error_code is not None):
            raise ValueError("error_code is set exactly when the verdict is refuse")
        if refuse and self.error_code not in DECISION_REFUSAL_CODES:
            raise ValueError(f"{self.error_code} is not a decision refusal code")
        return self


__all__: list[str] = ["ModelPrHandoffDecision"]
