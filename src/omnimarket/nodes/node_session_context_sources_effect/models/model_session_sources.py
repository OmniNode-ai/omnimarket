# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed prompt, citation and carry records of the session sources effect (OMN-21108, TA.6)."""

from __future__ import annotations

from enum import StrEnum, unique

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


@unique
class EnumSessionSourceFailure(StrEnum):
    """Why a session-source read failed. Each failure is the first token of `error_message`."""

    REPLAY_ROOT_MISSING = "REPLAY_ROOT_MISSING"
    CASE_NOT_FOUND = "CASE_NOT_FOUND"
    PIN_MISMATCH = "PIN_MISMATCH"
    NO_SOURCE_TIMESTAMP = "NO_SOURCE_TIMESTAMP"
    SCOPE_INVALID = "SCOPE_INVALID"
    FACTOR_NOT_SERVED = "FACTOR_NOT_SERVED"
    LIVE_MODE_NOT_BUILT = "LIVE_MODE_NOT_BUILT"


@unique
class EnumPromptOrigin(StrEnum):
    """How a prompt is known to be operator-typed.

    OPERATOR_PROMPT: an `operator_prompt` record (transcript origin human, journalled at Stop).
    PROMPT_UNCONFIRMED: a `prompt` record (UserPromptSubmit) that no Stop read after it
    classified; it is carried, because a carry set may hold more than the operator typed but
    never less.
    """

    OPERATOR_PROMPT = "operator_prompt"
    PROMPT_UNCONFIRMED = "prompt_unconfirmed"


@unique
class EnumCarryDisposition(StrEnum):
    CARRIED = "carried"
    RECORDED = "recorded"
    REVIEWED = "reviewed"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelSessionPrompt(_Frozen):
    """One operator prompt of a session, in capture order."""

    prompt_id: str = Field(min_length=1)
    capture_sequence: int = Field(ge=0)
    text: str
    said_at: AwareDatetime
    origin: EnumPromptOrigin
    source_ref: str = Field(min_length=1)


class ModelPromptCitation(_Frozen):
    """A recorded row covering the half-open character span [start, end) of one prompt."""

    prompt_id: str = Field(min_length=1)
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    row_ref: str = Field(min_length=1)

    @model_validator(mode="after")
    def _end_not_before_start(self) -> ModelPromptCitation:
        if self.end < self.start:
            raise ValueError(f"covers end {self.end} is before start {self.start}")
        return self


class ModelReviewedRange(_Frozen):
    """An operator review row naming the prompts first..last, inclusive, in capture order (D10)."""

    first_prompt_id: str = Field(min_length=1)
    last_prompt_id: str = Field(min_length=1)
    row_ref: str = Field(min_length=1)


class ModelCarryDecision(_Frozen):
    """What the carry rule did with one prompt, and the rows behind it."""

    prompt: ModelSessionPrompt
    disposition: EnumCarryDisposition
    covered_by: tuple[ModelPromptCitation, ...] = ()
    reviewed_by: str | None = None


__all__ = [
    "EnumCarryDisposition",
    "EnumPromptOrigin",
    "EnumSessionSourceFailure",
    "ModelCarryDecision",
    "ModelPromptCitation",
    "ModelReviewedRange",
    "ModelSessionPrompt",
]
