# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request, rule data and verdict of the question guard."""

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class EnumQuestionGuardVerdict(StrEnum):
    REFUSE = "refuse"
    ADMIT = "admit"


class EnumQuestionGuardTransport(StrEnum):
    IN_MEMORY = "in_memory"
    REDPANDA = "redpanda"


def _compiles(patterns: tuple[str, ...]) -> tuple[str, ...]:
    for pattern in patterns:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"invalid pattern {pattern!r}: {exc}") from None
    return patterns


class ModelQuestionGuardRule(BaseModel):
    """One class of question and the ruling that settles it.

    A ``refuse`` rule matches a question when every ``match_all`` pattern matches it.
    An ``admit`` rule matches when the question offers at least ``min_options`` options
    and every ``match_all`` pattern (often none) matches. Either acts only while a RULING
    row with the stamp ``ruling`` is in the request's ruling rows.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    ruling: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
    verdict: EnumQuestionGuardVerdict
    reason: str = Field(min_length=1)
    match_all: tuple[str, ...] = ()
    min_options: int = Field(default=0, ge=0)

    @field_validator("match_all")
    @classmethod
    def _patterns_compile(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _compiles(value)


class ModelQuestionGuardRules(BaseModel):
    """How questions are found and counted, and the rule classes; from the contract or an overlay."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    question_lead: str = Field(min_length=1)
    option_list: str = Field(min_length=1)
    option_list_separator: str = Field(min_length=1)
    alternative: str = Field(min_length=1)
    unmatched_reason: str = Field(min_length=1)
    rules: tuple[ModelQuestionGuardRule, ...] = ()

    @field_validator("question_lead", "option_list", "alternative")
    @classmethod
    def _pattern_compiles(cls, value: str) -> str:
        return _compiles((value,))[0]


class ModelQuestionGuardRequest(BaseModel):
    """A drafted reply, the ruling rows in force, and optionally the rule data to judge by."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reply_text: str
    ruling_rows: tuple[str, ...] = ()
    rules: ModelQuestionGuardRules | None = None


class ModelQuestionVerdict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    question: str
    options: int
    verdict: EnumQuestionGuardVerdict
    rule_id: str = ""
    ruling_id: str = ""
    reason: str


class ModelQuestionGuardResult(BaseModel):
    """``refuse`` with the first refused question's ruling and reason, else ``admit``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: EnumQuestionGuardVerdict
    rule_id: str = ""
    ruling_id: str = ""
    reason: str
    questions: tuple[ModelQuestionVerdict, ...] = ()


class ModelQuestionGuardTopics(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    command: str
    decided: str


__all__ = [
    "EnumQuestionGuardTransport",
    "EnumQuestionGuardVerdict",
    "ModelQuestionGuardRequest",
    "ModelQuestionGuardResult",
    "ModelQuestionGuardRule",
    "ModelQuestionGuardRules",
    "ModelQuestionGuardTopics",
    "ModelQuestionVerdict",
]
