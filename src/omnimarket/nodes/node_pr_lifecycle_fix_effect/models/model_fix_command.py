"""ModelPrLifecycleFixCommand — command to start PR lifecycle fix."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, NonNegativeInt


class EnumPrBlockReason(StrEnum):
    """Block reasons that drive fix routing.

    ci_failure                     → flaky/infra rerun via ``gh run rerun --failed``
    code_failure                   → lint/type/test failure, delegate to pr_polish
    receipt_failure                → OCC/receipt-gate failure, delegate to pr_polish
    conflict                       → merge conflict, resolve via ``gh pr update-branch``
    changes_requested              → review comment fix, delegate to pr_polish
    coderabbit                     → CR thread auto-reply via dispatch_coderabbit_reply
    deploy_gate_contract_not_found → deploy-gate failed because OCC contract YAML is
                                     missing; auto-create it via create_occ_contract
    receipt_evidence_source_autobind → Receipt Gate failed because the PR's
                                     Evidence-Source points at the product head SHA
                                     instead of an OCC source; autobind OCC receipt
                                     evidence via autobind_evidence_source (OMN-13317)
    """

    CI_FAILURE = "ci_failure"
    CODE_FAILURE = "code_failure"
    RECEIPT_FAILURE = "receipt_failure"
    CONFLICT = "conflict"
    CHANGES_REQUESTED = "changes_requested"
    CODERABBIT = "coderabbit"
    DEPLOY_GATE_CONTRACT_NOT_FOUND = "deploy_gate_contract_not_found"
    RECEIPT_EVIDENCE_SOURCE_AUTOBIND = "receipt_evidence_source_autobind"


class ModelPrLifecycleFixCommand(BaseModel):
    """Command to start PR lifecycle fix effect."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Fix run correlation ID.")
    pr_number: int = Field(..., description="PR number to remediate.", gt=0)
    repo: str = Field(..., description="GitHub repo slug (owner/repo).")
    block_reason: EnumPrBlockReason = Field(
        ..., description="Block reason driving the fix route."
    )
    ticket_id: str | None = Field(
        default=None, description="Linear ticket ID for context."
    )
    dry_run: bool = Field(default=False, description="Run without side effects.")
    requested_at: datetime = Field(..., description="When the command was issued.")
    changed_files: list[str] = Field(
        default_factory=list,
        description=(
            "PR changed-file paths, relative to repo root. Used by the "
            "trivial-infra OCC fast-path (OMN-13776) to decide whether a "
            "deploy_gate_contract_not_found fix can skip the full OCC "
            "receipt-chain. Empty/unknown never qualifies for the fast-path."
        ),
    )
    diff_total_lines: NonNegativeInt = Field(
        default=0,
        description=(
            "Total additions + deletions across changed_files. Used by the "
            "trivial-infra OCC fast-path size scoping (OMN-13776)."
        ),
    )


__all__: list[str] = ["EnumPrBlockReason", "ModelPrLifecycleFixCommand"]
