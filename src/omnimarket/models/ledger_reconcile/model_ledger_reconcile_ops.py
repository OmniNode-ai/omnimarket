# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The operation requests and results of the ledger-reconcile nodes (OMN-20677).

The compute node decides, the effect node reads, verifies and appends, and the
orchestrator carries one to the other, so the types between them live here and none
of the three imports another's models. Every effect result carries ``error``: a
read or probe that could not be made says so, and is never an empty answer a
decision would take for a clean ledger.
"""

from __future__ import annotations

from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.ledger_reconcile.model_ledger_reconcile import (
    ModelAppendOutcome,
    ModelPlannedAppend,
    ModelReconcileFacts,
    ModelReconcileParams,
    ModelReconcileSources,
    ModelReconcileWanted,
)

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class ModelReconcileDecideRequest(BaseModel):
    """The ledger as read, the facts verified so far and the command's bounds."""

    model_config = _FROZEN

    correlation_id: UUID = Field(default_factory=uuid4)
    params: ModelReconcileParams
    sources: ModelReconcileSources
    facts: ModelReconcileFacts = ModelReconcileFacts()


class ModelReconcileRenderRequest(BaseModel):
    """The same decision request, plus how each planned append went."""

    model_config = _FROZEN

    decide: ModelReconcileDecideRequest
    outcomes: tuple[ModelAppendOutcome, ...] = ()


class ModelReadSourcesRequest(BaseModel):
    """Read the ledger host. Unset paths come from OMNI_HOME and ONEX_LEDGER_PATH."""

    model_config = _FROZEN

    correlation_id: UUID = Field(default_factory=uuid4)
    ledger_path: str | None = None
    registry_root: str | None = None
    live_lanes: tuple[str, ...] = ()
    live_lanes_file: str | None = None


class ModelReadSourcesResult(BaseModel):
    model_config = _FROZEN

    correlation_id: UUID
    sources: ModelReconcileSources | None = None
    error: str = ""


class ModelVerifyEvidenceRequest(BaseModel):
    model_config = _FROZEN

    correlation_id: UUID = Field(default_factory=uuid4)
    wanted: ModelReconcileWanted
    github_org: str
    registry_root: str | None = None
    registry_name: str


class ModelVerifyEvidenceResult(BaseModel):
    model_config = _FROZEN

    correlation_id: UUID
    facts: ModelReconcileFacts = ModelReconcileFacts()
    error: str = ""


class ModelAppendRowsRequest(BaseModel):
    model_config = _FROZEN

    correlation_id: UUID = Field(default_factory=uuid4)
    rows: tuple[ModelPlannedAppend, ...]
    ledger_path: str | None = None


class ModelAppendRowsResult(BaseModel):
    model_config = _FROZEN

    correlation_id: UUID
    outcomes: tuple[ModelAppendOutcome, ...] = ()
    error: str = ""
