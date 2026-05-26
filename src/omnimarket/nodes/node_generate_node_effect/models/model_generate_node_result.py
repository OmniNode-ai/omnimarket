"""ModelGenerateNodeResult — result of a node generation run."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelGenerateNodeResult(BaseModel):
    """Result listing the files created by a generate-node run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Mirrors the command correlation ID.")
    node_name: str = Field(..., description="Name of the scaffolded node.")
    created_files: tuple[str, ...] = Field(
        default=(),
        description="Paths of files written to disk, relative to output_dir.",
    )
    output_dir: str = Field(..., description="Directory where files were written.")
    dry_run: bool = Field(
        default=False,
        description="True when no files were actually written (dry-run mode).",
    )


__all__: list[str] = ["ModelGenerateNodeResult"]
