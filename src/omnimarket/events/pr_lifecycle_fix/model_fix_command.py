"""ModelPrLifecycleFixCommand — command to start PR lifecycle fix."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, NonNegativeInt, model_validator

from omnimarket.events.occ_companion import EnumOccBatchMode
from omnimarket.events.pr_landing_companion import EnumPrLandingCompanionOp


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
    occ_batch_mode: EnumOccBatchMode = Field(
        default=EnumOccBatchMode.WINDOW,
        description=(
            "OCC companion grouping mode. Absent means window: one companion per "
            "product repository per batch window, shared by every open product "
            "PR of that repository whatever ticket its title cites (OMN-16336, "
            "operator ruling 2026-09-27T10:32:50Z). The merge-sweep receipt "
            "repair and the autobind publisher's default leave the field off, "
            "so the default is what they get. ticket groups only PRs citing the "
            "same single ticket. off (one companion per product PR) is asked "
            "for explicitly: by the conflicted-companion re-mint of a legacy "
            "per-PR branch, or by a repository that turned batching off, which "
            "queue health reports."
        ),
    )
    op: EnumPrLandingCompanionOp = Field(
        default=EnumPrLandingCompanionOp.DERIVE,
        description=(
            "Companion operation for the receipt_evidence_source_autobind block "
            "reason (OMN-19827, the PR landing workflow's companion seam): "
            "derive, regenerate or verify. Absent means derive, which is what "
            "every publisher that predates the field sends, so they keep "
            "working unchanged. The producer honours regenerate from "
            "OMN-19832: it re-mints the PR's own open companion from a fresh "
            "change-control base (or rebuilds the ticket batch companion under "
            "occ_batch_mode=ticket) without waiting for a product push. verify "
            "is not built yet and runs as derive."
        ),
    )
    command_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        description=(
            "Set by the PR landing workflow on the companion commands it "
            "issues (OMN-19832, plan revision 1 F4/F5): the id the landing row "
            "records as the command in flight. The producer echoes it on the "
            "typed companion outcome so the reducer can correlate the answer. "
            "Absent on every push-driven command."
        ),
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
    review_context_text: str = Field(
        default="",
        description=(
            "Concatenated PR title/body/CodeRabbit comment text, used by the "
            "delegation content denylist (WS-D/D2, OMN-13940) to refuse "
            "security/auth/crypto-adjacent fixes before routing to a "
            "delegated (non-Claude) fix path. Empty is always denied "
            "delegation-eligibility neutral (path/size checks still apply)."
        ),
    )

    @model_validator(mode="after")
    def _companion_op_needs_the_companion_route(self) -> Self:
        companion_route = (
            self.block_reason is EnumPrBlockReason.RECEIPT_EVIDENCE_SOURCE_AUTOBIND
        )
        if self.op is not EnumPrLandingCompanionOp.DERIVE and not companion_route:
            raise ValueError(
                f"op={self.op.value} is a companion operation and is only "
                "meaningful with block_reason=receipt_evidence_source_autobind, "
                f"got block_reason={self.block_reason.value}"
            )
        if self.command_id is not None and not companion_route:
            raise ValueError(
                "command_id names a companion command and is only meaningful "
                "with block_reason=receipt_evidence_source_autobind, got "
                f"block_reason={self.block_reason.value}"
            )
        return self


__all__: list[str] = ["EnumPrBlockReason", "ModelPrLifecycleFixCommand"]
