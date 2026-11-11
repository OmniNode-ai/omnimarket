# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models for the changed-path-live check."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_lab_proof_judge_compute.models.enum_judge_verdict import (
    EnumJudgeVerdict,
)
from omnimarket.nodes.node_lab_proof_judge_compute.models.enum_use_outcome import (
    EnumUseOutcome,
)
from omnimarket.nodes.node_lab_proof_judge_compute.models.model_substitute_receipt import (
    HEAD_PATTERN,
    ModelSubstituteReceipt,
)


class ModelChangedPathLiveRequest(BaseModel):
    """The input of the changed-path-live check."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(..., min_length=1)
    pr_number: int = Field(..., ge=1)
    head: str = Field(..., pattern=HEAD_PATTERN)
    changed_paths: tuple[str, ...] = Field(..., min_length=1)
    base_changed_path_live: bool
    contract_local_nodes: tuple[str, ...] = ()
    substitute_receipts: tuple[ModelSubstituteReceipt, ...] = ()


class ModelMatchedEntry(BaseModel):
    """One entry of the not-applicable declaration that matched a changed node."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    node: str = Field(..., min_length=1)
    runs_by: str = Field(..., min_length=1)
    reason: str = Field(..., min_length=1)
    substitute_how: str = Field(..., min_length=1)


class ModelFoundProof(BaseModel):
    """One substitute receipt that matched a changed node."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    node: str = Field(..., min_length=1)
    receipt_id: str = Field(..., min_length=1)
    host: str = Field(..., min_length=1)
    host_class: str = Field(..., min_length=1)
    command: str = Field(..., min_length=1)
    finished_at: str = Field(..., min_length=1)
    log_path: str = Field(..., min_length=1)


class ModelNotApplicableUse(BaseModel):
    """The not-applicable use: which nodes, receipts and outcome."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(..., min_length=1)
    pr_number: int = Field(..., ge=1)
    head: str = Field(..., min_length=1)
    class_id: str = Field(..., min_length=1)
    outcome: EnumUseOutcome
    nodes: tuple[str, ...] = ()
    receipt_ids: tuple[str, ...] = ()


class ModelChangedPathLiveResult(BaseModel):
    """The result of the changed-path-live check."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: EnumJudgeVerdict
    ok: bool
    class_id: str | None = None
    matched_entries: tuple[ModelMatchedEntry, ...] = ()
    substitute_proofs: tuple[ModelFoundProof, ...] = ()
    missing_proof_nodes: tuple[str, ...] = ()
    unmatched_nodes: tuple[str, ...] = ()
    evidence: str = Field(..., min_length=1)
    use: ModelNotApplicableUse | None = None
