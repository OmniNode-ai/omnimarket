"""ModelMergeCommand — input model for the merge effect node."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelMergeCommand(BaseModel):
    """Command to perform a git merge operation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo_path: str = Field(..., description="Path to the repository.")
    branch: str = Field(..., description="Branch name to merge into.")
    base_branch: str = Field(
        default="origin/main", description="Base branch to merge from."
    )
    dry_run: bool = Field(
        default=False, description="If true, test merge without committing."
    )


__all__ = ["ModelMergeCommand"]
