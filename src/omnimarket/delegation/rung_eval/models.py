# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contracts for nightly rung evaluations."""

from __future__ import annotations

import re
from datetime import date
from enum import StrEnum

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)


class EnumRungEvalStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    UNRESOLVED = "unresolved"


class ModelRungSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    rung_id: str
    kind: str
    base_url_env: str | None = None
    model_env: str | None = None
    api_key_env: str | None = None
    # A rung reached through ``onex delegate`` pinned to a backend, running as a
    # tenant that holds the provider key by reference (OMN-20974). It declares
    # no endpoint, model or key of its own.
    backend_id_env: str | None = None
    tenant_env: str | None = None

    @model_validator(mode="after")
    def _one_way_to_reach_the_rung(self) -> ModelRungSpec:
        if self.backend_id_env is not None:
            if self.tenant_env is None:
                raise ValueError(f"{self.rung_id}: a pinned rung names its tenant")
            if self.api_key_env is not None:
                raise ValueError(f"{self.rung_id}: a pinned rung holds no key")
        elif self.base_url_env is None or self.model_env is None:
            raise ValueError(f"{self.rung_id}: names no endpoint and no backend pin")
        return self


class ModelTransportResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    model_id: str
    latency_ms: int
    tenant_id: str | None = None
    credential_ref: str | None = None


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
    # The tenant that answered and the reference its key was read by, for a rung
    # run as a tenant. A reference, never a value (OMN-20974).
    tenant_id: str | None = None
    credential_ref: str | None = None


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
    # "no row" when the cell is absent; "unresolved: <detail>" when the only row
    # is a placeholder for a rung that was never called.
    reason: str = "no row"
