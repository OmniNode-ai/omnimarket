# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Immutable context-pack budget facts and decisions (OMN-21119, plan TA.18 AC1/AC3)."""

from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EnumDelegationContextPackStatus(StrEnum):
    """Context-pack budget and admission decisions for a delegation request."""

    NOT_REQUESTED = "not_requested"
    REQUEST_PACK = "request_pack"
    NO_ROOM = "no_room"
    ATTACH = "attach"
    REFUSE_EMPTY_HASH = "refuse_empty_hash"
    REFUSE_UNMEASURED_INPUT = "refuse_unmeasured_input"
    REFUSE_INPUT_OVERFLOW = "refuse_input_overflow"


class ModelDelegationContextPackBudgetRequest(BaseModel):
    """Backend token counts, the task class's profile and the pack answer if present."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(
        min_length=1, description="Correlation identifier for this delegation."
    )
    task_class: str = Field(
        min_length=1, description="Task class whose context-pack admission is decided."
    )
    context_profile: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_]*$",
        description="Context profile named by the task class; absent when none is requested.",
    )
    profile_token_budget: int | None = Field(
        default=None, ge=1, description="Token budget declared by the context profile."
    )
    composed_request_tokens: int = Field(
        ge=0,
        description="Outbound request composed without the pack, counted by the routed backend's tokenizer.",
    )
    max_grounded_input_tokens: int = Field(
        ge=1, description="Routed backend's grounded-input token ceiling."
    )
    token_estimator: str = Field(
        min_length=1,
        description="Tokenizer or conservative estimate used to count the composed request.",
    )
    context_pack_hash: str | None = Field(
        default=None,
        description="Pack orchestrator's answer, possibly empty; None means no answer yet.",
    )
    assembled_request_tokens: int | None = Field(
        default=None,
        ge=0,
        description="Full serialized outbound input including the pack, counted with token_estimator after assembly.",
    )

    @model_validator(mode="after")
    def validate_profile_budget(self) -> Self:
        """Require a context profile and its budget together, or neither."""
        if (self.context_profile is None) != (self.profile_token_budget is None):
            raise ValueError(
                "context_profile and profile_token_budget must be set together"
            )
        if self.assembled_request_tokens is not None:
            if self.context_profile is None or self.context_pack_hash is None:
                raise ValueError(
                    "assembled_request_tokens requires a profile and pack answer"
                )
            if self.assembled_request_tokens < self.composed_request_tokens:
                raise ValueError(
                    "assembled input cannot be smaller than the input without the pack"
                )
        return self


class ModelDelegationContextPackBudgetResult(BaseModel):
    """Pack budget and dispatch admission with the request facts used to decide them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(
        description="Correlation identifier for this delegation."
    )
    task_class: str = Field(
        description="Task class whose context-pack admission was decided."
    )
    context_profile: str | None = Field(
        description="Context profile named by the task class."
    )
    status: EnumDelegationContextPackStatus = Field(
        description="Context-pack budget and admission decision."
    )
    dispatch_allowed: bool = Field(
        description="Whether the delegation may be dispatched."
    )
    token_budget_override: int | None = Field(
        description="Pack budget bounded by remaining backend room and the profile budget."
    )
    context_pack_hash: str = Field(
        default="",
        description="Stripped pack hash to attach; empty when no pack is attached.",
    )
    composed_request_tokens: int = Field(
        description="Token count of the outbound request composed without the pack."
    )
    max_grounded_input_tokens: int = Field(
        description="Routed backend's grounded-input token ceiling."
    )
    token_estimator: str = Field(
        description="Tokenizer or conservative estimate used to count the composed request."
    )
    assembled_request_tokens: int | None = Field(
        description="Measured full serialized input including the pack, or None before measurement."
    )
    reason: str = Field(
        description="Factual reason for this context-pack admission decision."
    )
