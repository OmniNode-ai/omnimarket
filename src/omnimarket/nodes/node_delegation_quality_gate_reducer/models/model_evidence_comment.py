# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2026 OmniNode Team
"""Typed evidence-comment policy declared by the quality gate's contract.

The parsed form of the ``evidence_comment`` block in the node's
``contract.yaml``: how a draft's input is laid out (the criteria block and the
lane report), how an entry is recognised, and what counts as evidence. They are
contract facts, not handler constants.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelEvidenceCommentPolicy(BaseModel):
    """The contract-declared policy for ``criteria_covered`` and ``entry_cites_evidence``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    policy_version: str = Field(..., min_length=1)
    criteria_check_name: str = Field(..., min_length=1)
    evidence_check_name: str = Field(..., min_length=1)
    criteria_header_pattern: str = Field(
        ...,
        min_length=1,
        description="Python re pattern for the line that opens the criteria block.",
    )
    report_header_pattern: str = Field(
        ...,
        min_length=1,
        description="Python re pattern for the line that opens the lane report.",
    )
    criterion_pattern: str = Field(
        ...,
        min_length=1,
        description=(
            "Python re pattern, one match per criterion line, with named groups "
            "``label`` and ``text``."
        ),
    )
    entry_label_pattern: str = Field(
        ...,
        min_length=1,
        description=(
            "Python re pattern for the start of a draft entry, with a named "
            "group ``label``."
        ),
    )
    no_evidence_phrase: str = Field(
        ...,
        min_length=1,
        description="The literal entry a draft writes when the report holds none.",
    )
    evidence_token_patterns: tuple[str, ...] = Field(
        ...,
        min_length=1,
        description=(
            "Python re patterns for a token that is evidence: an identifier, a "
            "file or test name, a count, a timestamp."
        ),
    )
    restatement_overlap: float = Field(
        ...,
        gt=0.0,
        le=1.0,
        description=(
            "Share of an entry's words found in its criterion's text at or "
            "above which the entry restates the criterion."
        ),
    )
    min_word_length: int = Field(
        ..., ge=1, description="Words shorter than this are not compared."
    )
    max_reported: int = Field(..., ge=1)
