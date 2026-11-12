# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed rule, request and report for the required-and-absent reading of broker topics.

The node is the single reader of a contract's declared topics, in every shape.
It carries no effect: it only reports what the contracts declare.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class EnumRequiredAbsentVerdict(StrEnum):
    WORKING = "WORKING"
    NOT_WORKING = "NOT_WORKING"
    UNKNOWN = "UNKNOWN"


class ModelRequiredAbsentRule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    min_live_topics: int  # a live listing with fewer topics than this is a failed read


class ModelRequiredTopic(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    topic: str
    required_by: (
        str  # required_by names the contract, host serve unit or runtime that needs it
    )


class ModelRequiredAbsentRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    rule: ModelRequiredAbsentRule
    required: tuple[ModelRequiredTopic, ...]
    live_topics: (
        tuple[str, ...] | None
    )  # None means the broker's topic list was not read


class ModelAbsentTopic(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    topic: str
    required_by: tuple[str, ...]  # sorted, distinct


class ModelRequiredAbsentReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: EnumRequiredAbsentVerdict
    reason: str
    required_count: int  # distinct topics
    present_count: int
    absent: tuple[ModelAbsentTopic, ...]  # sorted by topic
