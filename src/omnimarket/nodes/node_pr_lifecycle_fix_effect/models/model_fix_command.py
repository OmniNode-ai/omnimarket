"""ModelPrLifecycleFixCommand — command to start PR lifecycle fix."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class EnumPrBlockReason(StrEnum):
    """Block reasons that drive fix routing.

    ci_failure       → flaky/infra rerun via ``gh run rerun --failed``
    code_failure     → lint/type/test failure, delegate to pr_polish
    receipt_failure  → OCC/receipt-gate failure, delegate to pr_polish
    conflict         → merge conflict, resolve via ``gh pr update-branch``
    changes_requested → review comment fix, delegate to pr_polish
    coderabbit       → CR thread auto-reply via dispatch_coderabbit_reply
    """

    CI_FAILURE = "ci_failure"
    CODE_FAILURE = "code_failure"
    RECEIPT_FAILURE = "receipt_failure"
    CONFLICT = "conflict"
    CHANGES_REQUESTED = "changes_requested"
    CODERABBIT = "coderabbit"


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


__all__: list[str] = ["EnumPrBlockReason", "ModelPrLifecycleFixCommand"]
