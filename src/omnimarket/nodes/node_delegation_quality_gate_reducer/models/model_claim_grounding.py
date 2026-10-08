# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2026 OmniNode Team
"""Typed claim-grounding policy declared by the quality gate's contract.

OMN-19199. The parsed form of the ``claim_grounding`` block in the node's
``contract.yaml``: the patterns that anchor a claim to an identifier, how an
answer is cut into clauses, the state groups a clause can assert, and the
failure policy. They are contract facts, not handler constants.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_identifier_grounding import (
    ModelIdentifierGroundingFailurePolicy,
)


class ModelClaimGroundingPolicy(BaseModel):
    """The contract-declared claim-grounding policy."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    policy_version: str = Field(..., min_length=1)
    check_name: str = Field(..., min_length=1)
    anchor_patterns: tuple[str, ...] = Field(
        ...,
        min_length=1,
        description="Python re patterns; a match is an identifier a clause is about.",
    )
    clause_split: str = Field(
        ...,
        min_length=1,
        description=(
            "Python re pattern cutting an answer into statements. An anchor "
            "never carries across this cut."
        ),
    )
    subclause_split: str = Field(
        ...,
        min_length=1,
        description=(
            "Python re pattern cutting a statement into clauses. A clause that "
            "cites no identifier inherits the anchors of the clause before it, "
            "so a state after a comma is still held to the identifier it "
            "follows."
        ),
    )
    source_section_start_patterns: tuple[str, ...] = Field(
        default=(),
        description=(
            "Python re patterns matching a source line that starts one report. "
            "Its lines form a grounding unit until the next report starts. "
            "Lines before the first report remain independent source rows."
        ),
    )
    excluded_answer_spans: tuple[str, ...] = Field(default=())
    imperative_subject_patterns: tuple[str, ...] = Field(
        default=(),
        description=(
            "Python re patterns, each with a named group ``verb``. The text the "
            "group matches is an imperative verb that opens a commit subject "
            "(``fix(OMN-1): resolve ...``): it names the change the subject "
            "makes, not a state the answer asserts, so it is read out of the "
            "answer before the clauses are cut (OMN-20492)."
        ),
    )
    state_groups: dict[str, tuple[str, ...]] = Field(
        ...,
        min_length=1,
        description=(
            "Group name -> word-start terms. A clause that uses any term asserts "
            "the group; the source supports it if any term of the group occurs."
        ),
    )
    unverified_markers: tuple[str, ...] = Field(default=())
    unverified_window: int = Field(default=40, ge=0)
    failure_policy: ModelIdentifierGroundingFailurePolicy = Field(
        default_factory=ModelIdentifierGroundingFailurePolicy
    )

    @field_validator(
        "anchor_patterns",
        "excluded_answer_spans",
        "unverified_markers",
        "source_section_start_patterns",
    )
    @classmethod
    def _patterns_compile(cls, patterns: tuple[str, ...]) -> tuple[str, ...]:
        for pattern in patterns:
            re.compile(pattern)
        return patterns

    @field_validator("imperative_subject_patterns")
    @classmethod
    def _subject_patterns_name_the_verb(
        cls, patterns: tuple[str, ...]
    ) -> tuple[str, ...]:
        for pattern in patterns:
            if "verb" not in re.compile(pattern).groupindex:
                raise ValueError(
                    f"imperative subject pattern declares no (?P<verb>...) group: {pattern!r}"
                )
        return patterns

    @field_validator("clause_split", "subclause_split")
    @classmethod
    def _split_compiles(cls, pattern: str) -> str:
        re.compile(pattern)
        return pattern

    @field_validator("state_groups")
    @classmethod
    def _groups_are_lowercase_and_non_empty(
        cls, groups: dict[str, tuple[str, ...]]
    ) -> dict[str, tuple[str, ...]]:
        for name, terms in groups.items():
            if not terms:
                raise ValueError(f"state group {name!r} declares no term")
            for term in terms:
                if term != term.lower() or not term.strip():
                    raise ValueError(f"state term must be lowercase: {term!r}")
        return groups


class ModelUngroundedClaim(BaseModel):
    """One state a clause asserts that the source does not support."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    group: str = Field(..., min_length=1)
    term: str = Field(..., min_length=1, description="The word the answer used.")
    anchor: str | None = Field(
        default=None, description="The identifier the clause was about, if any."
    )
    clause: str = Field(..., min_length=1)

    def rendered(self) -> str:
        """``deprioritized (OMN-19174)`` or bare ``blocked``."""
        return f"{self.group} ({self.anchor})" if self.anchor else self.group


class ModelClaimGroundingVerdict(BaseModel):
    """Outcome of evaluating claim grounding for one response."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evaluated: Literal[True, False]
    checked_count: int = Field(default=0, ge=0)
    ungrounded: tuple[ModelUngroundedClaim, ...] = Field(default=())
