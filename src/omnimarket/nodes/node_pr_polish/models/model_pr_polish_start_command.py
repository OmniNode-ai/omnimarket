"""ModelPrPolishStartCommand — command to start PR polish."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelPrPolishStartCommand(BaseModel):
    """Command to start PR polish."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Polish run correlation ID.")
    repo: str | None = Field(
        default=None, description="GitHub repo slug (owner/repo) for live polish."
    )
    pr_number: int | None = Field(default=None, description="PR number.")
    ticket_id: str | None = Field(
        default=None, description="Linear ticket ID for traceability."
    )
    required_clean_runs: int = Field(default=4, ge=1)
    max_iterations: int = Field(default=10, ge=1)
    skip_conflicts: bool = Field(default=False)
    skip_pr_review: bool = Field(default=False)
    skip_local_review: bool = Field(default=False)
    no_ci: bool = Field(default=False)
    no_push: bool = Field(default=False)
    no_automerge: bool = Field(default=False)
    dry_run: bool = Field(default=False)
    worktree_path: str | None = Field(
        default=None, description="Explicit worktree path override for live polish."
    )
    run_dir: str | None = Field(
        default=None, description="Explicit state dir for breadcrumbs and result.json."
    )
    requested_at: datetime = Field(..., description="When the command was issued.")


__all__: list[str] = ["ModelPrPolishStartCommand"]
