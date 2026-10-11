# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Command, phase call and terminal event of the hourly Linear-comments sweep (OMN-20680)."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

ISO_SECONDS = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")


class ModelLinearCommentsSweepRequest(BaseModel):
    """One sweep of the Linear comments addressed to the operator since an exact cutoff."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    correlation_id: UUID
    emitted_at: datetime
    tenant_id: str = Field(min_length=1)
    since: str
    facts: str = Field(
        min_length=1,
        description="The ground-truth table a delegated draft may cite; without it the drafter has none.",
    )
    task_type: Literal["document", "summarization", "reasoning"] = "document"

    @field_validator("since")
    @classmethod
    def exact_cutoff(cls, value: str) -> str:
        if not ISO_SECONDS.fullmatch(value):
            raise ValueError("since must be an exact UTC cutoff, YYYY-MM-DDTHH:MM:SSZ")
        return value


class ModelSweepPhaseRequest(BaseModel):
    """One model call the sweep asks of the delegation orchestrator."""

    model_config = ConfigDict(frozen=True)

    label: str
    phase: str
    task_type: Literal["agent_delegation", "document", "summarization", "reasoning"]
    model: str = ""
    effort: str = ""
    prompt: str
    max_tokens: int = Field(default=8192, gt=0)
    timeout_seconds: int = Field(default=600, gt=0)
    schema_definition: dict[str, JsonValue] | None = None


class ModelSweepPhaseReply(BaseModel):
    """What came back: the content, or the reason there is none."""

    model_config = ConfigDict(frozen=True)

    ok: bool
    failure: str = ""
    content: str = ""
    model_used: str = ""
    finish_reason: str | None = None
    truncated: bool | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    latency_ms: int = 0
    attempts: int | None = None
    escalations: int | None = None


class ModelSweepCollected(BaseModel):
    """The collector's answer: every comment of the window and the proof the feed was live."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    items: list[dict[str, JsonValue]]
    positive_control: str = Field(min_length=1)


class ModelSweepVerdict(BaseModel):
    """The verifier's answer on one draft."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    accepted: bool
    rejection_reason: str = ""
    draft: dict[str, JsonValue]


class ModelSweepWritten(BaseModel):
    """What the finalize phase wrote."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    report_path: str
    metrics_path: str
    rendered_by: Literal["delegated", "fallback"]
    metrics_rows: int = Field(ge=0)


class ModelLinearCommentsSweepResult(BaseModel):
    """The window's counts, its held tally and where the report went."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    correlation_id: UUID
    tenant_id: str
    linear_file: str
    metrics_file: str
    since: str
    collected: int = Field(ge=0)
    needing: int = Field(ge=0)
    refused: int = Field(ge=0)
    refused_by_class: dict[str, int]
    accepted: int = Field(ge=0)
    held: int = Field(ge=0)
    held_by_class: dict[str, int]
    degraded: bool
    control: str
    written: ModelSweepWritten
