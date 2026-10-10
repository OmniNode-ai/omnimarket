# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Caller-supplied PR facts and pure title decisions (OMN-20885)."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class EnumPrTitleCheckReason(StrEnum):
    """The first matching branch of the reusable check-title step."""

    EMPTY_TITLE = "empty_title"
    BOT_AUTHOR = "bot_author"
    DEPENDENCY_BUMP = "dependency_bump"
    RELEASE = "release"
    TICKET_REFERENCE = "ticket_reference"
    MISSING_TICKET_REFERENCE = "missing_ticket_reference"


class ModelPrTitleCheckRequest(BaseModel):
    """PR title and author supplied by the caller."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str
    author: str


class ModelPrTitleCheckResult(BaseModel):
    """Admission decision, shell exit code and exact stdout lines."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    admitted: bool
    reason: EnumPrTitleCheckReason
    exit_code: int
    output_lines: tuple[str, ...]
