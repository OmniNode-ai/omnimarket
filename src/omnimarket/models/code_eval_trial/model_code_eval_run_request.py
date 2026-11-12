# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The run command of the code eval trial (OMN-23627, plan revision 44, task E1).

Exemplar: ``nodes/node_pr_landing_orchestrator/models/``. Field names say what
is measured: model arms and candidates. Every number the run uses comes from the
overlay or the contract, never from this request.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelCodeEvalCandidate(BaseModel):
    """One merged change whose ticket contract binds the check (rule 3)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str = Field(min_length=1)
    merge_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    contract_id: str = Field(pattern=r"^OMN-[0-9]+$")


class ModelCodeEvalArm(BaseModel):
    """One model backend, pinned for a whole sequence (rule 11)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str = Field(min_length=1)
    backend_id: str = Field(min_length=1)


class ModelCodeEvalRunRequest(BaseModel):
    """``onex.cmd.omnimarket.code-eval-run-requested.v1``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    tenant_id: str = Field(min_length=1)
    harness_revision: str = Field(min_length=1)
    publish_cells: bool
    candidates: tuple[ModelCodeEvalCandidate, ...] = Field(min_length=1)
    arms: tuple[ModelCodeEvalArm, ...] = Field(min_length=1)


__all__ = ["ModelCodeEvalArm", "ModelCodeEvalCandidate", "ModelCodeEvalRunRequest"]
