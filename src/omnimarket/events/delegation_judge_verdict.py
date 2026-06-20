# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Reproducible judge verdict event models for non-verifiable delegation output."""

from __future__ import annotations

import hashlib
import json
import re
from enum import StrEnum
from typing import Any, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class EnumDelegationJudgeVerdict(StrEnum):
    """Controlled verdict vocabulary for auditable judge outcomes."""

    PASS = "pass"
    BORDERLINE = "borderline"
    FAIL = "fail"
    JUDGE_FAILED = "judge_failed"


_SHA256_RE = re.compile(r"^sha256:[a-f0-9]{64}$")


class ModelDelegationJudgeVerdictEvent(BaseModel):
    """Durable judge verdict event for non-verifiable delegation classes.

    The event is controlled, reproducible, and auditable: replay compares the
    identity bundle and event hash for the same prompt/input/rubric envelope. It
    does not claim LLM inference itself is deterministic.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    correlation_id: UUID = Field(..., description="Delegation correlation id.")
    task_type: str = Field(..., min_length=1)
    score_source: str = Field(default="reproducible_judge", frozen=True)
    judge_model: str = Field(..., min_length=1)
    judge_model_version: str = Field(..., min_length=1)
    judge_provider: str = Field(..., min_length=1)
    rubric_id: str = Field(..., min_length=1)
    rubric_hash: str = Field(..., description="sha256:<hex> rubric digest.")
    prompt_hash: str = Field(..., description="sha256:<hex> prompt digest.")
    input_hash: str = Field(..., description="sha256:<hex> judged input digest.")
    temperature: float = Field(..., ge=0.0, le=2.0)
    judge_node_version: str = Field(..., min_length=1)
    reasoning_hash: str = Field(..., description="sha256:<hex> reasoning digest.")
    verdict: EnumDelegationJudgeVerdict = Field(...)
    actual_score: float | None = Field(..., ge=0.0, le=1.0)
    failure_kind: str | None = Field(default=None)
    failure_message: str | None = Field(default=None)
    event_hash: str = Field(..., description="sha256:<hex> event identity digest.")

    @field_validator("rubric_hash", "prompt_hash", "input_hash", "reasoning_hash")
    @classmethod
    def _validate_hash(cls, value: str) -> str:
        if not _SHA256_RE.match(value):
            raise ValueError("hash fields must use sha256:<64 lowercase hex>")
        return value

    @field_validator("event_hash")
    @classmethod
    def _validate_event_hash(cls, value: str) -> str:
        if not _SHA256_RE.match(value):
            raise ValueError("event_hash must use sha256:<64 lowercase hex>")
        return value

    @model_validator(mode="after")
    def _validate_failure_semantics(self) -> Self:
        if self.verdict is EnumDelegationJudgeVerdict.JUDGE_FAILED:
            if self.actual_score is not None:
                raise ValueError("judge_failed verdict must not carry a zero score")
            if not self.failure_kind or not self.failure_message:
                raise ValueError(
                    "judge_failed verdict requires failure_kind and failure_message"
                )
        elif self.actual_score is None:
            raise ValueError("non-failure judge verdicts require actual_score")
        return self

    def identity_bundle(self) -> dict[str, Any]:
        """Return the replay identity bundle, excluding only event_hash."""
        return {
            "correlation_id": str(self.correlation_id),
            "task_type": self.task_type,
            "score_source": self.score_source,
            "judge_model": self.judge_model,
            "judge_model_version": self.judge_model_version,
            "judge_provider": self.judge_provider,
            "rubric_id": self.rubric_id,
            "rubric_hash": self.rubric_hash,
            "prompt_hash": self.prompt_hash,
            "input_hash": self.input_hash,
            "temperature": self.temperature,
            "judge_node_version": self.judge_node_version,
            "reasoning_hash": self.reasoning_hash,
            "verdict": self.verdict.value,
            "actual_score": self.actual_score,
            "failure_kind": self.failure_kind,
            "failure_message": self.failure_message,
        }

    def compute_event_hash(self) -> str:
        """Compute the stable replay hash for this verdict identity."""
        return sha256_json(self.identity_bundle())


def sha256_text(value: str) -> str:
    """Return a canonical sha256 digest string for raw text."""
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_json(value: dict[str, Any]) -> str:
    """Return a canonical sha256 digest string for JSON-serializable data."""
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def build_delegation_judge_verdict_event(
    *,
    correlation_id: UUID,
    task_type: str,
    judge_model: str,
    judge_model_version: str,
    judge_provider: str,
    rubric_id: str,
    rubric_hash: str,
    prompt: str,
    judged_input: str,
    temperature: float,
    judge_node_version: str,
    reasoning: str,
    verdict: EnumDelegationJudgeVerdict,
    actual_score: float | None,
    failure_kind: str | None = None,
    failure_message: str | None = None,
) -> ModelDelegationJudgeVerdictEvent:
    """Build a judge verdict event and attach its reproducible event hash."""
    base = {
        "correlation_id": correlation_id,
        "task_type": task_type,
        "judge_model": judge_model,
        "judge_model_version": judge_model_version,
        "judge_provider": judge_provider,
        "rubric_id": rubric_id,
        "rubric_hash": rubric_hash,
        "prompt_hash": sha256_text(prompt),
        "input_hash": sha256_text(judged_input),
        "temperature": temperature,
        "judge_node_version": judge_node_version,
        "reasoning_hash": sha256_text(reasoning),
        "verdict": verdict,
        "actual_score": actual_score,
        "failure_kind": failure_kind,
        "failure_message": failure_message,
    }
    event_hash = sha256_json(
        ModelDelegationJudgeVerdictEvent(
            **base,
            event_hash="sha256:" + "0" * 64,
        ).identity_bundle()
    )
    return ModelDelegationJudgeVerdictEvent(**base, event_hash=event_hash)


__all__ = [
    "EnumDelegationJudgeVerdict",
    "ModelDelegationJudgeVerdictEvent",
    "build_delegation_judge_verdict_event",
    "sha256_json",
    "sha256_text",
]
