# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contracts for nightly rung evaluations."""

from __future__ import annotations

import re
from datetime import date
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator


class EnumRungEvalStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    UNRESOLVED = "unresolved"


class ModelRungSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    rung_id: str
    kind: str
    base_url_env: str
    model_env: str
    api_key_env: str | None = None


class ModelTransportResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    model_id: str
    latency_ms: int


class ModelRungEvalCase(BaseModel):
    # Fixture provenance (source) is metadata, not a grading input.
    model_config = ConfigDict(frozen=True, extra="ignore")

    case_id: str
    task_class: str
    prompt: str
    answer_key: dict[str, str] | None = None
    ticket_ids: list[str] | None = None
    known_ids: list[str] = Field(default_factory=list, validate_default=True)
    max_groups: int | None = None

    @field_validator("known_ids")
    @classmethod
    def derive_known_ids(cls, _value: list[str], info: ValidationInfo) -> list[str]:
        prompt = info.data.get("prompt")
        return (
            sorted(set(re.findall(r"\bOMN-\d+\b", prompt)))
            if info.data.get("ticket_ids") is not None and isinstance(prompt, str)
            else []
        )


class ModelRungEvalRow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    night: date
    rung_id: str
    task_class: str
    case_id: str
    status: EnumRungEvalStatus
    score: float
    passed: bool
    latency_ms: int | None = None
    model_id: str | None = None
    detail: str


class ModelGrade(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    score: float
    passed: bool
    detail: str


class ModelMissingNight(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    night: date
    rung_id: str
    task_class: str
    case_id: str
