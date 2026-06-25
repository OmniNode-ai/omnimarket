# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Per-phase bus commands + completion facts for the closeout FSM (OMN-13413).

The closeout ORCHESTRATOR owns phase sequencing but dispatches every phase OVER
THE BUS — it never constructs sibling handlers in-process and never runs an
in-process FSM loop. Each command starts a phase; each completion fact carries
the deterministic result the orchestrator threads into the next command and,
finally, into ``ModelCloseoutReceipt``.

The deploy phase reuses ``node_redeploy_orchestrator`` (its ``redeploy-start``
command + ``redeploy-completed`` fact) and the proof-matrix phase reuses
``node_golden_chain_sweep`` + ``node_integration_sweep`` — so these models cover
only the closeout-specific preflight, fitness-gate, and proof-matrix-rollup
edges.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.events.runtime_closeout import (
    EnumProofSet,
    ModelImageRow,
    ModelMigrationLedgerEntry,
    ModelProofCellVerdict,
)
from omnimarket.events.runtime_deployment import EnumRuntimeLane


class ModelCloseoutPreflightCommand(BaseModel):
    """Command: run the read-only preflight (identity/broker/projection/migration).

    Preflight is read-only — it never mutates the lane. It establishes the
    artifact identity + migration ledger + rollback target the receipt records,
    and is the first gate: a failing preflight blocks the closeout before any
    deploy.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Closeout run correlation ID.")
    runtime_lane: EnumRuntimeLane = Field(..., description="Lane to inspect read-only.")
    requested_by: str = Field(
        default="node_runtime_closeout_orchestrator",
        description="Identity label.",
    )


class ModelCloseoutPreflightFact(BaseModel):
    """Fact: the read-only preflight result.

    ``ready`` False blocks the closeout. ``images`` + ``migration_ledger`` +
    ``rollback_target`` feed the receipt; the orchestrator never re-derives them.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Closeout run correlation ID.")
    runtime_lane: EnumRuntimeLane = Field(..., description="Lane that was inspected.")
    ready: bool = Field(
        default=False, description="Whether preflight passed (lane fit for deploy)."
    )
    images: tuple[ModelImageRow, ...] = Field(
        default_factory=tuple, description="Observed SHA / image identity rows."
    )
    migration_ledger: tuple[ModelMigrationLedgerEntry, ...] = Field(
        default_factory=tuple, description="Observed migration ledger."
    )
    rollback_target: str | None = Field(
        default=None, description="Previous-good digest for the rollback plan."
    )
    detail: str = Field(default="", description="Preflight detail / failure reason.")


class ModelCloseoutFitnessGateCommand(BaseModel):
    """Command: evaluate the fresh-deploy fitness gate (OMN-13410 sibling).

    The fitness gate decides whether the artifact is fit to deploy fresh (no
    drift between built artifact and dev HEAD). It runs AFTER preflight and
    BEFORE deploy, so a drifted artifact is rejected before any lane mutation.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Closeout run correlation ID.")
    runtime_lane: EnumRuntimeLane = Field(..., description="Target lane.")
    images: tuple[ModelImageRow, ...] = Field(
        default_factory=tuple, description="Artifact identity rows from preflight."
    )


class ModelCloseoutFitnessGateFact(BaseModel):
    """Fact: fresh-deploy fitness gate decision."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Closeout run correlation ID.")
    fit: bool = Field(
        default=False, description="Whether the artifact is fit to deploy fresh."
    )
    reason: str = Field(default="", description="Gate decision reason.")


class ModelCloseoutProofMatrixCommand(BaseModel):
    """Command: run the runtime proof matrix for the requested proof set.

    The orchestrator dispatches this to the reused golden-chain / integration
    sweep nodes; each cell is proven with a fresh CID -> typed terminal ->
    projection readback. ``proof_set`` selects the required slice or the full
    matrix.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Closeout run correlation ID.")
    runtime_lane: EnumRuntimeLane = Field(..., description="Lane to prove against.")
    proof_set: EnumProofSet = Field(
        default=EnumProofSet.REQUIRED, description="Required slice or full matrix."
    )
    cells: tuple[str, ...] = Field(
        default_factory=tuple,
        description="Cell names to prove (derived from proof_set).",
    )


class ModelCloseoutProofMatrixFact(BaseModel):
    """Fact: per-cell runtime-proof verdicts from the proof-matrix phase."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Closeout run correlation ID.")
    cell_verdicts: tuple[ModelProofCellVerdict, ...] = Field(
        default_factory=tuple, description="Per-cell runtime-proof verdicts."
    )
    detail: str = Field(default="", description="Proof-matrix detail.")


__all__: list[str] = [
    "ModelCloseoutFitnessGateCommand",
    "ModelCloseoutFitnessGateFact",
    "ModelCloseoutPreflightCommand",
    "ModelCloseoutPreflightFact",
    "ModelCloseoutProofMatrixCommand",
    "ModelCloseoutProofMatrixFact",
]
