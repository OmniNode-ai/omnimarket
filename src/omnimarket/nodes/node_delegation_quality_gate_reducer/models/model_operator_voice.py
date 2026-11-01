# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2026 OmniNode Team
"""Typed operator-voice policy declared by the quality gate's contract.

The parsed form of the ``operator_voice`` block in the node's ``contract.yaml``:
the phrases that grant approval, consent or a decision in the first person, the
words that negate or defer one, and the failure policy. They are contract facts,
not handler constants.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_identifier_grounding import (
    ModelIdentifierGroundingFailurePolicy,
)


class ModelOperatorVoicePolicy(BaseModel):
    """The contract-declared operator-voice policy."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    policy_version: str = Field(..., min_length=1)
    check_name: str = Field(..., min_length=1)
    grant_groups: dict[str, tuple[str, ...]] = Field(
        ...,
        min_length=1,
        description=(
            "Group name -> Python re patterns. A draft phrase that matches a "
            "pattern grants the group; the source holds the grant if any "
            "pattern of the same group matches an un-negated phrase in it."
        ),
    )
    negation_patterns: tuple[str, ...] = Field(
        default=(),
        description=(
            "Python re patterns. A match inside the negation window before a "
            "grant phrase means the phrase refuses, defers or denies the grant "
            "instead of making it."
        ),
    )
    negation_window: int = Field(default=40, ge=0)
    clause_split: str = Field(..., min_length=1)
    excluded_draft_spans: tuple[str, ...] = Field(default=())
    failure_policy: ModelIdentifierGroundingFailurePolicy = Field(
        default_factory=ModelIdentifierGroundingFailurePolicy
    )

    @field_validator("negation_patterns", "excluded_draft_spans")
    @classmethod
    def _patterns_compile(cls, patterns: tuple[str, ...]) -> tuple[str, ...]:
        for pattern in patterns:
            re.compile(pattern)
        return patterns

    @field_validator("clause_split")
    @classmethod
    def _split_compiles(cls, pattern: str) -> str:
        re.compile(pattern)
        return pattern

    @field_validator("grant_groups")
    @classmethod
    def _group_patterns_compile(
        cls, groups: dict[str, tuple[str, ...]]
    ) -> dict[str, tuple[str, ...]]:
        for name, patterns in groups.items():
            if not patterns:
                raise ValueError(f"grant group {name!r} declares no pattern")
            for pattern in patterns:
                re.compile(pattern)
        return groups


class ModelOperatorVoiceFinding(BaseModel):
    """One grant the draft makes that the source does not hold."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    check: Literal["operator_voice"] = "operator_voice"
    group: str = Field(..., min_length=1)
    phrase: str = Field(..., min_length=1, description="The words the draft used.")
    clause: str = Field(..., min_length=1)

    def rendered(self) -> str:
        """``approve: "I approve"``."""
        return f'{self.group}: "{self.phrase}"'


class ModelOperatorVoiceVerdict(BaseModel):
    """Outcome of evaluating operator voice for one draft."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evaluated: bool
    checked_count: int = Field(default=0, ge=0)
    findings: tuple[ModelOperatorVoiceFinding, ...] = Field(default=())
