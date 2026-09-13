# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2026 OmniNode Team
"""Typed identifier-grounding policy declared by the quality gate's contract.

OMN-18297. The gate had no check that compared a response against the input it
was derived from, so a local-tier delegation over ~18K input tokens emitted
pull-request citations absent from its own source and scored 1.0. These models
are the parsed form of the ``identifier_grounding`` block in the node's
``contract.yaml`` -- the classes, the lookup form per class, the unverified
markers, and the failure policy are contract facts, not handler constants.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

EnumUngroundedPolicy = Literal["fail"]
EnumSourceUnavailablePolicy = Literal["skip_and_record"]
EnumGroundingBand = Literal["heuristic"]
EnumAnswerSegmentRule = Literal["after_last_terminator"]


class ModelIdentifierClass(BaseModel):
    """One declared class of identifier whose occurrences must be grounded."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    class_name: str = Field(..., min_length=1, description="Contract class name.")
    pattern: str = Field(
        ...,
        min_length=1,
        description=(
            "Python re pattern with a named group 'token' naming the substring "
            "to ground."
        ),
    )
    grounding_form: str = Field(
        ...,
        min_length=1,
        description=(
            "Template the source is searched for, with '{token}' substituted "
            "by the matched token."
        ),
    )
    prefix_match: bool = Field(
        default=False,
        description=(
            "Ground a token that is a prefix of a longer source occurrence "
            "(abbreviated commit shas)."
        ),
    )
    description: str = Field(default="", description="Human-readable note.")

    @field_validator("pattern")
    @classmethod
    def _pattern_declares_token_group(cls, pattern: str) -> str:
        compiled = re.compile(pattern)
        if "token" not in compiled.groupindex:
            raise ValueError(
                f"identifier class pattern must declare a named group 'token': {pattern!r}"
            )
        return pattern

    @field_validator("grounding_form")
    @classmethod
    def _grounding_form_uses_token(cls, grounding_form: str) -> str:
        if "{token}" not in grounding_form:
            raise ValueError(
                f"grounding_form must contain '{{token}}': {grounding_form!r}"
            )
        return grounding_form


class ModelIdentifierGroundingFailurePolicy(BaseModel):
    """What the gate does with an ungrounded identifier or an absent source."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    band: EnumGroundingBand = Field(default="heuristic")
    on_ungrounded: EnumUngroundedPolicy = Field(default="fail")
    on_source_unavailable: EnumSourceUnavailablePolicy = Field(
        default="skip_and_record"
    )
    max_reported: int = Field(default=25, ge=1)


class ModelIdentifierGroundingAnswerSegment(BaseModel):
    """How the answer is isolated from a leaked reasoning trace (OMN-18278)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stray_trace_terminator: str = Field(..., min_length=1)
    rule: EnumAnswerSegmentRule = Field(default="after_last_terminator")


class ModelIdentifierGroundingPolicy(BaseModel):
    """The contract-declared identifier-grounding policy."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    policy_version: str = Field(..., min_length=1)
    check_name: str = Field(..., min_length=1)
    classes: tuple[ModelIdentifierClass, ...] = Field(..., min_length=1)
    unverified_markers: tuple[str, ...] = Field(default=())
    unverified_window: int = Field(default=40, ge=0)
    failure_policy: ModelIdentifierGroundingFailurePolicy = Field(
        default_factory=ModelIdentifierGroundingFailurePolicy
    )
    answer_segment: ModelIdentifierGroundingAnswerSegment


class ModelUngroundedIdentifier(BaseModel):
    """One identifier occurrence with no traceable occurrence in the source."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    class_name: str = Field(..., min_length=1)
    identifier: str = Field(
        ...,
        min_length=1,
        description="The identifier as the response spelled it.",
    )
    looked_up_as: str = Field(
        ...,
        min_length=1,
        description="The exact string searched for in the grounding source.",
    )


class ModelIdentifierGroundingVerdict(BaseModel):
    """Outcome of evaluating identifier grounding for one response."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evaluated: bool = Field(
        ...,
        description=(
            "False when no grounding source was available. A non-evaluated "
            "verdict is recorded as a skipped check, never as a pass."
        ),
    )
    checked_count: int = Field(default=0, ge=0)
    ungrounded: tuple[ModelUngroundedIdentifier, ...] = Field(default=())


__all__: list[str] = [
    "EnumAnswerSegmentRule",
    "EnumGroundingBand",
    "EnumSourceUnavailablePolicy",
    "EnumUngroundedPolicy",
    "ModelIdentifierClass",
    "ModelIdentifierGroundingAnswerSegment",
    "ModelIdentifierGroundingFailurePolicy",
    "ModelIdentifierGroundingPolicy",
    "ModelIdentifierGroundingVerdict",
    "ModelUngroundedIdentifier",
]
