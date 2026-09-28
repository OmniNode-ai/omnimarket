# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2026 OmniNode Team
"""Typed number-grounding policy declared by the quality gate's contract.

OMN-19529. The parsed form of the ``numeric_grounding`` block in the node's
``contract.yaml``: the pattern a digit claim is read with, the spelled number
words, the answer spans that are not claims, the unverified markers and the
failure policy. They are contract facts, not handler constants.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_identifier_grounding import (
    ModelIdentifierGroundingFailurePolicy,
)


def _compiles(pattern: str) -> str:
    re.compile(pattern)
    return pattern


class ModelNumericGroundingFailurePolicy(ModelIdentifierGroundingFailurePolicy):
    """What the gate does when numeric grounding cannot be evaluated."""

    on_source_without_numbers: Literal["skip_and_record"] = Field(
        default="skip_and_record"
    )


class ModelNumericGroundingPolicy(BaseModel):
    """The contract-declared number-grounding policy."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    policy_version: str = Field(..., min_length=1)
    check_name: str = Field(..., min_length=1)
    claim_pattern: str = Field(
        ...,
        min_length=1,
        description="Python re pattern with a named group 'token': one digit claim.",
    )
    source_pattern: str = Field(
        ..., min_length=1, description="Python re pattern: one digit run in the source."
    )
    source_part_separators: str = Field(
        ...,
        min_length=1,
        description="Python re pattern splitting a source digit run into parts.",
    )
    excluded_answer_spans: tuple[str, ...] = Field(
        default=(),
        description="Python re patterns for answer spans that carry no claim.",
    )
    number_words: dict[str, int] = Field(
        ...,
        min_length=1,
        description="Spelled numbers read as claims in the answer and as values in the source.",
    )
    source_only_words: dict[str, int] = Field(
        default_factory=dict,
        description="Spelled numbers read in the source only, never as an answer claim.",
    )
    compound_separator: str = Field(
        ...,
        min_length=1,
        description="Python re pattern joining a tens word to a unit word.",
    )
    spelled_modifier_follower: str = Field(
        ...,
        min_length=1,
        description=(
            "Python re pattern matched after an answer's spelled number to identify "
            "a hyphen-joined modifier."
        ),
    )
    unverified_markers: tuple[str, ...] = Field(default=())
    unverified_window: int = Field(default=40, ge=0)
    failure_policy: ModelNumericGroundingFailurePolicy = Field(
        default_factory=ModelNumericGroundingFailurePolicy
    )

    @field_validator("claim_pattern")
    @classmethod
    def _claim_pattern_declares_token_group(cls, pattern: str) -> str:
        if "token" not in re.compile(pattern).groupindex:
            raise ValueError(
                f"claim_pattern must declare a named group 'token': {pattern!r}"
            )
        return pattern

    @field_validator(
        "source_pattern",
        "source_part_separators",
        "compound_separator",
        "spelled_modifier_follower",
    )
    @classmethod
    def _pattern_compiles(cls, pattern: str) -> str:
        return _compiles(pattern)

    @field_validator("excluded_answer_spans", "unverified_markers")
    @classmethod
    def _patterns_compile(cls, patterns: tuple[str, ...]) -> tuple[str, ...]:
        for pattern in patterns:
            _compiles(pattern)
        return patterns

    @field_validator("number_words", "source_only_words")
    @classmethod
    def _words_are_lowercase_and_non_negative(
        cls, words: dict[str, int]
    ) -> dict[str, int]:
        for word, value in words.items():
            if word != word.lower() or not word.isalpha():
                raise ValueError(f"number word must be lowercase letters: {word!r}")
            if value < 0:
                raise ValueError(f"number word value must be >= 0: {word!r}={value}")
        return words


class ModelUngroundedNumber(BaseModel):
    """One number the answer states with no occurrence in the source."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    value: str = Field(..., min_length=1, description="Canonical value, e.g. '9'.")
    as_written: str = Field(
        ..., min_length=1, description="The claim as the answer spelled it."
    )

    def rendered(self) -> str:
        """``9`` for a digit claim, ``9 (Nine)`` for a spelled one."""
        if self.as_written == self.value:
            return self.value
        return f"{self.value} ({self.as_written})"


class ModelNumericGroundingVerdict(BaseModel):
    """Outcome of evaluating number grounding for one response."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evaluated: bool = Field(
        ...,
        description=(
            "False when no grounding source was available or the source stated no "
            "number. A non-evaluated verdict is recorded as a skipped check, never "
            "as a pass."
        ),
    )
    checked_count: int = Field(default=0, ge=0)
    ungrounded: tuple[ModelUngroundedNumber, ...] = Field(default=())


EnumNameResolutionBand = Literal["deterministic"]


class ModelNameResolutionFailurePolicy(BaseModel):
    """What the gate does with an unresolved name or an absent source."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    band: EnumNameResolutionBand = Field(default="deterministic")
    on_source_unavailable: Literal["skip_and_record"] = Field(default="skip_and_record")
    max_reported: int = Field(default=25, ge=1)


class ModelNameResolutionPolicy(BaseModel):
    """The contract-declared name-resolution policy for code classes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    policy_version: str = Field(..., min_length=1)
    check_name: str = Field(..., min_length=1)
    python_fence_tags: tuple[str, ...] = Field(..., min_length=1)
    source_word_pattern: str = Field(..., min_length=1)
    implicit_module_names: tuple[str, ...] = Field(default=())
    failure_policy: ModelNameResolutionFailurePolicy = Field(
        default_factory=ModelNameResolutionFailurePolicy
    )

    @field_validator("source_word_pattern")
    @classmethod
    def _pattern_compiles(cls, pattern: str) -> str:
        return _compiles(pattern)


class ModelNameResolutionVerdict(BaseModel):
    """Outcome of evaluating name resolution for one response."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evaluated: bool = Field(
        ...,
        description=(
            "False when no grounding source was available. A non-evaluated "
            "verdict is recorded as a skipped check, never as a pass."
        ),
    )
    unresolved: tuple[str, ...] = Field(default=())


__all__: list[str] = [
    "EnumNameResolutionBand",
    "ModelNameResolutionFailurePolicy",
    "ModelNameResolutionPolicy",
    "ModelNameResolutionVerdict",
    "ModelNumericGroundingFailurePolicy",
    "ModelNumericGroundingPolicy",
    "ModelNumericGroundingVerdict",
    "ModelUngroundedNumber",
]
