# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Arm-stage commands and append-only observations; case and truth schemas are shared."""

from __future__ import annotations

from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.compaction_replay.model_compaction_manifest import (
    ModelLedgerFile,
)
from omnimarket.models.compaction_replay.model_ground_truth import (
    ModelGroundTruthResult,
)
from omnimarket.models.compaction_replay.model_transcript_manifest import (
    ModelTranscriptCase,
)

Arm = Literal["stock_rerun", "mod", "stock_recorded", "oracle", "empty", "inverted"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelPremiseEvidence(_Frozen):
    manual_compaction: bool
    transcript_retained: bool
    evidence: str = Field(min_length=1)

    @property
    def accepted(self) -> bool:
        return self.manual_compaction and self.transcript_retained


class ModelUsage(_Frozen):
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    post_tokens: int | None = Field(default=None, ge=0)
    wall_ms: float | None = Field(default=None, ge=0)
    measurement: Literal["measured", "estimated", "unavailable"] = "unavailable"


class ModelCompactionObservation(_Frozen):
    reply: Literal["native", "installed", "refused", "unavailable"]
    context: str | None = None
    usage: ModelUsage = Field(default_factory=ModelUsage)
    evidence: str = Field(min_length=1)
    custom_boundary_evidence: tuple[str, ...] = ()
    transcript_retained: bool = True
    package_hash: str | None = None
    adapter_artifacts: tuple[str, ...] = ()


class ModelArmRunRequest(_Frozen):
    correlation_id: UUID = Field(default_factory=uuid4)
    case: ModelTranscriptCase
    transcript_prefix: bytes
    ledger_files: tuple[ModelLedgerFile, ...]
    recorded_summary: str
    ground_truth: ModelGroundTruthResult | None = None
    arms: tuple[Arm, ...] = ("stock_rerun", "mod", "stock_recorded")
    repeats: int = Field(default=1, ge=1, le=100)
    premise: ModelPremiseEvidence
    # For inverted controls the caller supplies the predecessor of the entire
    # effective item (target AND amenders). Keys are ground-truth item keys.
    predecessors: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _unique_arms(self) -> ModelArmRunRequest:
        if not self.arms or len(set(self.arms)) != len(self.arms):
            raise ValueError("arms must be nonempty and unique")
        if (
            self.ground_truth is not None
            and self.ground_truth.session_id != self.case.session_id
        ):
            raise ValueError("ground truth is for another session")
        return self


class ModelArmRecord(_Frozen):
    case_key: str
    arm: Arm
    repeat_index: int
    status: Literal["OK", "INVALID", "REFUSED", "UNAVAILABLE"]
    reason: str | None = None
    context: str | None = None
    context_hash: str | None = None
    initial_state_hash: str
    prefix_hash: str
    ledger_hash: str
    compaction_input_hash: str
    native_fallback: bool = False
    usage: ModelUsage = Field(default_factory=ModelUsage)
    evidence: str
    custom_boundary_evidence: tuple[str, ...] = ()
    package_hash: str | None = None
    artifact_path: str
    adapter_artifacts: tuple[str, ...] = ()

    @property
    def scorable(self) -> bool:
        return self.status == "OK" and self.context is not None


class ModelArmRunResult(_Frozen):
    correlation_id: UUID
    case_key: str
    premise: ModelPremiseEvidence
    records: tuple[ModelArmRecord, ...]
    stock_comparison: Literal["stock_rerun", "stock_recorded"]
    comparison_label: Literal["FRESH_STOCK", "RECORDED_STOCK"]
    planted_safeguard: Literal["AVAILABLE", "NOT_ESTABLISHED"]
    aa_control: Literal["compactor", "answerer_only"]
    mod_attempts: int
    mod_fallbacks: int
    mod_fallback_rate: float
    report_path: str
