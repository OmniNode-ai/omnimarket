# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared domain types for the runtime-closeout orchestration (OMN-13413).

The runtime-closeout orchestrator turns the overnight hand-run pipeline into a
canonical omnimarket capability. Its bus-native FSM walks:

    preflight (read-only) -> fresh-deploy fitness gate -> deploy (reuse
    node_redeploy_orchestrator) -> runtime proof matrix (reuse
    node_golden_chain_sweep + node_integration_sweep) -> ModelCloseoutReceipt.

These types are the wire contract the orchestrator emits/consumes plus the
terminal ``ModelCloseoutReceipt`` external consumers read. The deploy lane
vocabulary (``EnumRuntimeLane``) is reused from ``runtime_deployment`` so a
closeout and a redeploy speak the same lane language.
"""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.events.runtime_deployment import EnumRuntimeLane

__all__ = [
    "PROOF_MATRIX_CELLS",
    "REQUIRED_PROOF_CELLS",
    "EnumCloseoutPhase",
    "EnumCloseoutRecommendation",
    "EnumProofClass",
    "EnumProofSet",
    "EnumProofVerdict",
    "ModelCloseoutReceipt",
    "ModelImageRow",
    "ModelMigrationLedgerEntry",
    "ModelProofCellSpec",
    "ModelProofCellVerdict",
]


class EnumCloseoutPhase(StrEnum):
    """FSM phases for the runtime-closeout workflow.

    The orchestrator owns this sequence but never runs an in-process loop; each
    phase is advanced by consuming the prior phase's completion fact off the bus.
    ``COMPLETED`` / ``BLOCKED`` / ``FAILED`` are terminal.
    """

    IDLE = "idle"
    PREFLIGHT = "preflight"
    FITNESS_GATE = "fitness_gate"
    DEPLOY = "deploy"
    PROOF_MATRIX = "proof_matrix"
    RECEIPT = "receipt"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"


TERMINAL_CLOSEOUT_PHASES: frozenset[EnumCloseoutPhase] = frozenset(
    {
        EnumCloseoutPhase.COMPLETED,
        EnumCloseoutPhase.BLOCKED,
        EnumCloseoutPhase.FAILED,
    }
)


class EnumProofClass(StrEnum):
    """How a proof-matrix cell is weighted in the recommendation.

    ``REQUIRED`` cells must pass for a ``CUSTOMER_BETA`` recommendation;
    ``STRETCH`` cells inform but do not block; ``RESEARCH`` cells are exploratory
    and never block.
    """

    REQUIRED = "required"
    STRETCH = "stretch"
    RESEARCH = "research"


class EnumProofSet(StrEnum):
    """Which slice of the proof matrix a caller asks the orchestrator to run."""

    REQUIRED = "required"
    FULL = "full"


class EnumProofVerdict(StrEnum):
    """Per-cell runtime-proof outcome."""

    PASS = "pass"
    FAIL = "fail"
    SKIP = "skip"
    PENDING = "pending"


class EnumCloseoutRecommendation(StrEnum):
    """Terminal recommendation rolled up from the per-cell verdicts."""

    CUSTOMER_BETA = "customer_beta"
    INTERNAL_INTEGRATION = "internal_integration"
    HOLD = "hold"


class ModelProofCellSpec(BaseModel):
    """A single cell in the runtime proof matrix.

    A cell names a runtime behaviour to prove (delegation, SEA, context,
    gate-zero, savings, cross-feature) and its weight class. Proving a cell means
    a fresh correlation id flows to a typed terminal event and a projection
    readback — the orchestrator dispatches that proof to the reused
    golden-chain / integration sweep nodes; it does not run probes in-process.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    cell: str = Field(..., description="Proof-matrix cell name, e.g. 'delegation'.")
    proof_class: EnumProofClass = Field(
        ..., description="Whether the cell is required / stretch / research."
    )
    description: str = Field(
        default="", description="What runtime behaviour the cell proves."
    )


# The canonical proof matrix (report §8 cells). Classification per the DoD:
# delegation + SEA + gate-zero are required; context + savings are stretch;
# cross-feature is research.
PROOF_MATRIX_CELLS: tuple[ModelProofCellSpec, ...] = (
    ModelProofCellSpec(
        cell="delegation",
        proof_class=EnumProofClass.REQUIRED,
        description="Cheapest-first delegation routes + meters a real terminal.",
    ),
    ModelProofCellSpec(
        cell="sea",
        proof_class=EnumProofClass.REQUIRED,
        description="Self-extension agent escalation reaches an up-tier terminal.",
    ),
    ModelProofCellSpec(
        cell="gate_zero",
        proof_class=EnumProofClass.REQUIRED,
        description="Quality gate at tier zero produces a typed verdict.",
    ),
    ModelProofCellSpec(
        cell="context",
        proof_class=EnumProofClass.STRETCH,
        description="Context-injection path renders into the terminal.",
    ),
    ModelProofCellSpec(
        cell="savings",
        proof_class=EnumProofClass.STRETCH,
        description="Cost-savings projection reflects the routed terminal.",
    ),
    ModelProofCellSpec(
        cell="cross_feature",
        proof_class=EnumProofClass.RESEARCH,
        description="Cross-feature interaction surfaced end to end.",
    ),
)

REQUIRED_PROOF_CELLS: tuple[str, ...] = tuple(
    spec.cell
    for spec in PROOF_MATRIX_CELLS
    if spec.proof_class is EnumProofClass.REQUIRED
)


class ModelProofCellVerdict(BaseModel):
    """The runtime-proof outcome for one matrix cell."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    cell: str = Field(..., description="Proof-matrix cell name.")
    proof_class: EnumProofClass = Field(..., description="Weight class of the cell.")
    verdict: EnumProofVerdict = Field(
        default=EnumProofVerdict.PENDING, description="Per-cell runtime-proof result."
    )
    correlation_id: UUID | None = Field(
        default=None,
        description="Fresh CID used to prove this cell (None when not yet run).",
    )
    terminal_event: str | None = Field(
        default=None, description="Typed terminal event topic observed for the cell."
    )
    projection_readback: str | None = Field(
        default=None, description="Projection surface read back for the cell."
    )
    detail: str = Field(default="", description="Human-readable proof detail.")


class ModelImageRow(BaseModel):
    """One built-artifact identity row in the closeout receipt.

    Captures the SHA -> image-digest binding the closeout proved, so a reader can
    verify the running artifact came from the dev HEAD the receipt cites
    (cross-cutting root cause: artifact identity drifting from dev HEAD).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    service: str = Field(..., description="Service / image name.")
    git_sha: str = Field(..., description="Git SHA the artifact was built from.")
    image_digest: str | None = Field(
        default=None, description="Pinned image digest of the built artifact."
    )
    image_ref: str | None = Field(default=None, description="Mutable image reference.")


class ModelMigrationLedgerEntry(BaseModel):
    """A single migration the closeout observed applied or pending."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    migration_id: str = Field(..., description="Migration identifier / prefix.")
    applied: bool = Field(
        default=False, description="Whether the migration was applied on the lane."
    )
    detail: str = Field(default="", description="Migration status detail.")


class ModelCloseoutReceipt(BaseModel):
    """Terminal closeout receipt (the ORCHESTRATOR's recorded truth).

    Carries the SHA / image table, migration ledger, per-cell verdicts, rollback
    plan, residual risk, and the rolled-up recommendation. Emitted on the
    terminal ``closeout-completed`` event so external consumers read one durable
    artifact instead of replaying the FSM.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Closeout run correlation ID.")
    runtime_lane: EnumRuntimeLane = Field(
        ..., description="Lane the closeout ran against."
    )
    final_phase: EnumCloseoutPhase = Field(
        ..., description="Terminal FSM phase (completed | blocked | failed)."
    )
    images: tuple[ModelImageRow, ...] = Field(
        default_factory=tuple, description="SHA / image identity table."
    )
    migration_ledger: tuple[ModelMigrationLedgerEntry, ...] = Field(
        default_factory=tuple, description="Migration application ledger."
    )
    cell_verdicts: tuple[ModelProofCellVerdict, ...] = Field(
        default_factory=tuple, description="Per-cell runtime-proof verdicts."
    )
    rollback_plan: str = Field(
        default="", description="Rollback target / plan recorded for the lane."
    )
    residual_risk: str = Field(
        default="", description="Residual risk after the closeout."
    )
    recommendation: EnumCloseoutRecommendation = Field(
        default=EnumCloseoutRecommendation.HOLD,
        description="Rolled-up recommendation.",
    )
    error_message: str | None = Field(
        default=None, description="Failure detail when final_phase is failed/blocked."
    )

    def recompute_recommendation(self) -> EnumCloseoutRecommendation:
        """Roll the per-cell verdicts up into a recommendation.

        - Any REQUIRED cell FAILED, or a REQUIRED cell missing/not-PASS ->
          ``HOLD`` is too strong only for a hard failure; a partial run caps at
          ``INTERNAL_INTEGRATION`` (below).
        - ``HOLD`` when any REQUIRED cell explicitly FAILED.
        - ``CUSTOMER_BETA`` requires the FULL matrix proven: every canonical
          matrix cell (``PROOF_MATRIX_CELLS``) has a non-FAIL verdict present and
          every REQUIRED cell PASSED. A ``required``-only proof set therefore caps
          at ``INTERNAL_INTEGRATION`` because the stretch / research cells were
          never proven.
        - ``INTERNAL_INTEGRATION`` otherwise (required cells green but full matrix
          not proven).
        """
        verdict_by_cell = {v.cell: v for v in self.cell_verdicts}
        required = [
            v for v in self.cell_verdicts if v.proof_class is EnumProofClass.REQUIRED
        ]
        if any(v.verdict is EnumProofVerdict.FAIL for v in required):
            return EnumCloseoutRecommendation.HOLD

        # Customer-beta needs the whole canonical matrix proven (no FAIL, and
        # every cell has a verdict present). A required-only run is missing the
        # stretch/research cells -> caps below.
        full_matrix_proven = all(
            (cell := spec.cell) in verdict_by_cell
            and verdict_by_cell[cell].verdict
            in (EnumProofVerdict.PASS, EnumProofVerdict.SKIP)
            for spec in PROOF_MATRIX_CELLS
        )
        all_required_passed = bool(required) and all(
            v.verdict is EnumProofVerdict.PASS for v in required
        )
        if full_matrix_proven and all_required_passed:
            return EnumCloseoutRecommendation.CUSTOMER_BETA
        return EnumCloseoutRecommendation.INTERNAL_INTEGRATION
