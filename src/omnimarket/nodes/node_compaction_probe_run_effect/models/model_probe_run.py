# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed input and output of the compaction probe run node (answerer, judge, calibration).

The cases are typed here rather than imported from the probe builder node: a case is a
probe text, the context it is asked against, and the ruling facts the judge compares an
answer with. The caller derives them (the probe builder and the ground truth are other
nodes); this node reads neither.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "EnumControl",
    "EnumProbeVerdict",
    "EnumRunMode",
    "EnumRunStatus",
    "ModelCalibrationReport",
    "ModelCalibrationRequest",
    "ModelControlTally",
    "ModelProbeCase",
    "ModelProbeManifest",
    "ModelProbeRunRequest",
    "ModelProbeRunResult",
    "ModelProbeRunSettings",
    "ModelScoredAnswer",
]


class EnumRunMode(StrEnum):
    """RECORD may call a model for a key with no valid recording; REPLAY never does."""

    RECORD = "RECORD"
    REPLAY = "REPLAY"


class EnumControl(StrEnum):
    """Which control context a case was built for (plan section 3.6, controls 2 to 4)."""

    ORACLE = "ORACLE"
    EMPTY = "EMPTY"
    INVERTED = "INVERTED"
    ARM = "ARM"


class EnumProbeVerdict(StrEnum):
    """Verdict on one answer; UNAVAILABLE can never establish parity or a pass."""

    RECALLED = "RECALLED"
    ABSENT = "ABSENT"
    CONTRADICTED = "CONTRADICTED"
    UNAVAILABLE = "UNAVAILABLE"


class EnumRunStatus(StrEnum):
    COMPLETED = "COMPLETED"
    CALIBRATION_REQUIRED = "CALIBRATION_REQUIRED"


def _not_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be blank")
    return value


class ModelProbeRunSettings(BaseModel):
    """Decoding settings; part of every recording key."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    temperature: float = Field(default=0.0, ge=0.0)
    max_tokens: int = Field(default=512, gt=0)

    def canonical(self) -> str:
        return json.dumps(self.model_dump(), sort_keys=True, separators=(",", ":"))


class ModelProbeManifest(BaseModel):
    """The experiment's one immutable manifest; its digest is part of every key."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate_id: str
    node_commits: tuple[str, ...]
    served_model_ids: tuple[str, ...]
    prompt_hash: str
    probe_set_hash: str
    continuation_prompt_hash: str
    caps_data_hash: str
    recovery_list_hash: str
    preregistration_hash: str

    def digest(self) -> str:
        body = json.dumps(self.model_dump(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(body.encode("utf-8")).hexdigest()


class ModelProbeCase(BaseModel):
    """One probe against one context, with the ruling facts the judge compares against."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str
    control: EnumControl = EnumControl.ARM
    stamp: str
    probe_text: str
    context: str = Field(description="The compacted or control context; may be empty.")
    question: str
    quote: str
    predecessor_text: str | None = None

    @field_validator("case_id", "stamp", "probe_text", "question", "quote")
    @classmethod
    def _required(cls, value: str) -> str:
        return _not_blank(value)


class _ModelRunBase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(..., min_length=1)
    mode: EnumRunMode
    recording_root: str = Field(..., min_length=1, description="The replay root.")
    manifest: ModelProbeManifest
    answerer_model_id: str = Field(..., min_length=1)
    judge_model_id: str = Field(..., min_length=1)
    compactor_model_ids: tuple[str, ...] = ()
    settings: ModelProbeRunSettings = ModelProbeRunSettings()
    repeats: int = Field(default=3, ge=1)
    cases: tuple[ModelProbeCase, ...] = Field(..., min_length=1)

    @model_validator(mode="after")
    def _distinct_models(self) -> _ModelRunBase:
        if self.answerer_model_id == self.judge_model_id:
            raise ValueError("the answerer and the judge must be two different models")
        if self.judge_model_id in self.compactor_model_ids:
            raise ValueError("the judge must differ from any compactor model")
        ids = [case.case_id for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("case ids must be unique")
        return self


class ModelCalibrationRequest(_ModelRunBase):
    """Run the judge over the control cases and report its calibration."""


class ModelControlTally(BaseModel):
    """Verdict counts of one control and the bound that gates the judge."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    control: EnumControl
    expected: EnumProbeVerdict
    n: int
    recalled: int
    absent: int
    contradicted: int
    unavailable: int
    rate: float = Field(description="Share of n carrying the expected verdict.")
    wilson_lower: float = Field(description="95 percent Wilson lower bound of rate.")


class ModelCalibrationReport(BaseModel):
    """The judge's calibration on controls 2 to 4; no scoring starts without a pass."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    manifest_digest: str
    answerer_model_id: str
    judge_model_id: str
    oracle: ModelControlTally
    empty: ModelControlTally
    inverted: ModelControlTally
    failures: tuple[str, ...]
    passed: bool


class ModelProbeRunRequest(_ModelRunBase):
    """Answer and judge arm cases; refused without a passing calibration report."""

    calibration_report: ModelCalibrationReport | None = None


class ModelScoredAnswer(BaseModel):
    """One answer to one case at one repeat, and its judged verdict."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str
    repeat_index: int
    answer: str | None
    answer_attempt: int | None
    verdict: EnumProbeVerdict
    judge_attempt: int | None


class ModelProbeRunResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    status: EnumRunStatus
    reason: str | None = None
    results: tuple[ModelScoredAnswer, ...] = ()
