# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed input and output of the compaction arm producer (plan section 3.2, task CM.10).

A case is one frozen prefix (transcript up to the boundary plus the ledger as it stood) taken
from the replay root. Every arm of a case is a fork of that one prefix in its own directory with
its own configuration directory, so the arms differ only at the compaction. The compacted
context, the usage and the hashes of each arm are recorded; an arm that cannot be run says why
as a typed status and is never silently dropped.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.context_install_confirm import EnumBoundaryKind

__all__ = [
    "EnumArm",
    "EnumArmKind",
    "EnumArmStatus",
    "EnumArmTrack",
    "EnumPremise",
    "EnumReplySource",
    "EnumStockBranch",
    "ModelAdapterOutput",
    "ModelArmAdapterSpec",
    "ModelArmCase",
    "ModelArmRecord",
    "ModelArmRunRequest",
    "ModelArmRunResult",
    "ModelFork",
    "ModelPremiseReport",
    "ModelRemovedCase",
]

CaseId = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class EnumArm(StrEnum):
    """The six arms of plan section 3.2."""

    STOCK_RERUN = "stock_rerun"
    MOD = "mod"
    STOCK_RECORDED = "stock_recorded"
    ORACLE = "oracle"
    EMPTY = "empty"
    INVERTED = "inverted"


class EnumArmTrack(StrEnum):
    """Arms of different tracks share no worktree and no configuration."""

    STOCK = "stock"
    MOD = "mod"
    CONTROL = "control"


class EnumArmKind(StrEnum):
    """How an arm's compacted context comes into being."""

    NATIVE_COMPACTION = "native_compaction"
    PINNED_PACK_BUILD = "pinned_pack_build"
    RECORDED_SUMMARY = "recorded_summary"
    CONTROL = "control"


class EnumArmStatus(StrEnum):
    """RECORDED is the only scorable status."""

    RECORDED = "RECORDED"
    INVALID = "INVALID"
    REFUSED = "REFUSED"
    UNAVAILABLE = "UNAVAILABLE"


class EnumPremise(StrEnum):
    """A premise that is not PASS is not established; UNVERIFIED never passes."""

    PASS = "PASS"
    FAIL = "FAIL"
    UNVERIFIED = "UNVERIFIED"


class EnumReplySource(StrEnum):
    MOD = "MOD"
    NATIVE = "NATIVE"


class EnumStockBranch(StrEnum):
    """RERUN: stock_rerun is the stock arm. RECORDED_STOCK: it was refused (refusal branch)."""

    RERUN = "RERUN"
    RECORDED_STOCK = "RECORDED_STOCK"


class ModelArmAdapterSpec(BaseModel):
    """One row of the contract's arm_adapters roster."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    arm: EnumArm
    track: EnumArmTrack
    kind: EnumArmKind
    calls_model: bool


class ModelArmCase(BaseModel):
    """One frozen prefix, as files under the replay root, and the arm inputs built from it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: CaseId
    prefix_path: str = Field(
        ..., min_length=1, description="Transcript up to the boundary."
    )
    ledger_path: str = Field(
        ..., min_length=1, description="Ledger bytes at the boundary."
    )
    recorded_summary: str | None = Field(
        default=None,
        description="The native summary already in the transcript; None when unverified.",
    )
    oracle_context: str = ""
    inverted_context: str = ""


class ModelArmRunRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(..., min_length=1)
    replay_root: str = Field(..., min_length=1)
    run_root: str = Field(
        ..., min_length=1, description="Forks and records are written here."
    )
    repeat_index: int = Field(default=0, ge=0)
    arms: tuple[EnumArm, ...] = (
        EnumArm.STOCK_RERUN,
        EnumArm.MOD,
        EnumArm.STOCK_RECORDED,
    )
    cases: tuple[ModelArmCase, ...] = Field(..., min_length=1)

    @model_validator(mode="after")
    def _coherent(self) -> ModelArmRunRequest:
        if len({case.case_id for case in self.cases}) != len(self.cases):
            raise ValueError("case ids must be unique")
        if len(set(self.arms)) != len(self.arms) or not self.arms:
            raise ValueError("arms must be a non-empty set without repeats")
        return self


class ModelFork(BaseModel):
    """What an adapter is handed: one arm's own copy of one case, in its own directories."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str
    arm: EnumArm
    track: EnumArmTrack
    root: str
    prefix_path: str
    ledger_path: str
    config_dir: str


class ModelAdapterOutput(BaseModel):
    """What a compaction adapter returns for one fork."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    compacted_context: str | None
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    reply_source: EnumReplySource = EnumReplySource.NATIVE
    boundary_kind: EnumBoundaryKind = EnumBoundaryKind.NATIVE
    custom_boundary_evidence: tuple[str, ...] = ()


class ModelPremiseReport(BaseModel):
    """The two headless premises CM.10 falsifies first."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    manual_compaction: EnumPremise
    fork_keeps_transcript: EnumPremise

    @property
    def passed(self) -> bool:
        return (
            self.manual_compaction is EnumPremise.PASS
            and self.fork_keeps_transcript is EnumPremise.PASS
        )


class ModelArmRecord(BaseModel):
    """One arm of one case: its hashes, its status and, when recorded, its context and usage."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str
    arm: EnumArm
    track: EnumArmTrack
    repeat_index: int
    status: EnumArmStatus
    reason: str | None = None
    prefix_sha256: Sha256
    ledger_sha256: Sha256
    initial_state_hash: Sha256
    compaction_input_hash: Sha256
    fork_root: str
    compacted_context_sha256: Sha256 | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    reply_source: EnumReplySource | None = None
    boundary_kind: EnumBoundaryKind | None = None
    attempt: int | None = None

    @property
    def scorable(self) -> bool:
        return self.status is EnumArmStatus.RECORDED


class ModelRemovedCase(BaseModel):
    """A case removed from every arm for an infrastructure fault, with its typed reason."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str
    reason: str


class ModelArmRunResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    premises: ModelPremiseReport
    stock_branch: EnumStockBranch
    stock_rerun_refused: bool
    refusal_statement: str | None = Field(
        default=None,
        description="Printed in the report whenever stock_rerun is REFUSED.",
    )
    report_label: str | None = None
    planted_stratum_safeguard: str | None = None
    records: tuple[ModelArmRecord, ...] = ()
    removed_cases: tuple[ModelRemovedCase, ...] = ()
    mod_fallback_count: int = 0
    mod_recorded_count: int = 0
    mod_fallback_rate: float | None = None
