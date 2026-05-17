"""ModelCloseOutState and EnumCloseOutPhase for the close-out pipeline FSM."""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class EnumCloseOutPhase(StrEnum):
    """FSM phases for the close-out pipeline.

    Phase Transitions mirror the closeout phases in
    omniclaude/scripts/closeout-phase-contract.yaml, excluding build-loop phases.
        Any -> FAILED: Circuit breaker tripped or unrecoverable error
    """

    IDLE = "idle"
    A1_MERGE_SWEEP = "a1_merge_sweep"
    A2_DEPLOY_PLUGIN = "a2_deploy_plugin"
    A2B_PLUGIN_REFRESH = "a2b_plugin_refresh"
    A3_START_ENV = "a3_start_env"
    B1_RUNTIME_SWEEP = "b1_runtime_sweep"
    B2_DATA_FLOW_SWEEP = "b2_data_flow_sweep"
    B3_DATABASE_SWEEP = "b3_database_sweep"
    B4B_DATABASE_SWEEP = "b4b_database_sweep"
    B4B_DATABASE_SWEEP_RETRY = "b4b_database_sweep_retry"
    B4B_DATA_FLOW_SWEEP = "b4b_data_flow_sweep"
    B4B_DATA_FLOW_SWEEP_RETRY = "b4b_data_flow_sweep_retry"
    B4B_RUNTIME_SWEEP = "b4b_runtime_sweep"
    B4B_RUNTIME_SWEEP_RETRY = "b4b_runtime_sweep_retry"
    B5_INTEGRATION = "b5_integration"
    B6_CONTRACT_VERIFY = "b6_contract_verify"
    C1_RELEASE = "c1_release"
    C2_REDEPLOY = "c2_redeploy"
    D3_DASHBOARD_SWEEP = "d3_dashboard_sweep"
    E1_FOUNDATION_TESTS = "e1_foundation_tests"
    E2_PIPELINE_TESTS = "e2_pipeline_tests"
    E4_GOLDEN_CHAIN = "e4_golden_chain"
    E4_GOLDEN_CHAIN_RETRY = "e4_golden_chain_retry"
    E3_DASHBOARD_TESTS = "e3_dashboard_tests"
    DONE = "done"
    FAILED = "failed"


CLOSE_OUT_PHASE_ORDER: tuple[EnumCloseOutPhase, ...] = (
    EnumCloseOutPhase.A1_MERGE_SWEEP,
    EnumCloseOutPhase.A2_DEPLOY_PLUGIN,
    EnumCloseOutPhase.A2B_PLUGIN_REFRESH,
    EnumCloseOutPhase.A3_START_ENV,
    EnumCloseOutPhase.B1_RUNTIME_SWEEP,
    EnumCloseOutPhase.B2_DATA_FLOW_SWEEP,
    EnumCloseOutPhase.B3_DATABASE_SWEEP,
    EnumCloseOutPhase.B4B_DATABASE_SWEEP,
    EnumCloseOutPhase.B4B_DATABASE_SWEEP_RETRY,
    EnumCloseOutPhase.B4B_DATA_FLOW_SWEEP,
    EnumCloseOutPhase.B4B_DATA_FLOW_SWEEP_RETRY,
    EnumCloseOutPhase.B4B_RUNTIME_SWEEP,
    EnumCloseOutPhase.B4B_RUNTIME_SWEEP_RETRY,
    EnumCloseOutPhase.B5_INTEGRATION,
    EnumCloseOutPhase.B6_CONTRACT_VERIFY,
    EnumCloseOutPhase.C1_RELEASE,
    EnumCloseOutPhase.C2_REDEPLOY,
    EnumCloseOutPhase.D3_DASHBOARD_SWEEP,
    EnumCloseOutPhase.E1_FOUNDATION_TESTS,
    EnumCloseOutPhase.E2_PIPELINE_TESTS,
    EnumCloseOutPhase.E4_GOLDEN_CHAIN,
    EnumCloseOutPhase.E4_GOLDEN_CHAIN_RETRY,
    EnumCloseOutPhase.E3_DASHBOARD_TESTS,
)

TERMINAL_PHASES: frozenset[EnumCloseOutPhase] = frozenset(
    {EnumCloseOutPhase.DONE, EnumCloseOutPhase.FAILED}
)


def next_phase(current: EnumCloseOutPhase) -> EnumCloseOutPhase:
    """Return the next phase in the close-out progression."""
    if current == EnumCloseOutPhase.IDLE:
        return CLOSE_OUT_PHASE_ORDER[0]
    if current == CLOSE_OUT_PHASE_ORDER[-1]:
        return EnumCloseOutPhase.DONE
    if current in TERMINAL_PHASES:
        msg = f"No next phase from terminal state: {current}"
        raise ValueError(msg)

    idx = CLOSE_OUT_PHASE_ORDER.index(current)
    return CLOSE_OUT_PHASE_ORDER[idx + 1]


class ModelCloseOutState(BaseModel):
    """Immutable FSM state for the close-out pipeline."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Close-out run correlation ID.")
    current_phase: EnumCloseOutPhase = Field(
        default=EnumCloseOutPhase.IDLE, description="Current FSM phase."
    )
    consecutive_failures: int = Field(default=0, ge=0)
    max_consecutive_failures: int = Field(default=3, ge=1)
    dry_run: bool = Field(default=False)
    prs_merged: int = Field(default=0, ge=0)
    prs_polished: int = Field(default=0, ge=0)
    error_message: str | None = Field(default=None)


__all__: list[str] = [
    "CLOSE_OUT_PHASE_ORDER",
    "TERMINAL_PHASES",
    "EnumCloseOutPhase",
    "ModelCloseOutState",
    "next_phase",
]
