# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelCrawlFilesystemRequest — input model for the content-type-aware filesystem crawler."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ModelCrawlFilesystemRequest(BaseModel):
    """Input request for HandlerCrawlFilesystem.

    Supports two modes:
      - Directory-walk mode: set ``root_paths`` to walk recursively.
      - File-list mode: set ``file_list`` to crawl an explicit set of files.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    root_paths: list[str] = Field(
        default_factory=list,
        description="Absolute directory paths to walk recursively.",
    )
    file_list: list[str] = Field(
        default_factory=list,
        description="Explicit file paths to crawl (canary / corpus-manifest mode).",
    )
    exclude_dirs: list[str] = Field(
        default_factory=lambda: ["__pycache__", ".git", "node_modules", ".venv"],
        description="Directory names to skip during walk.",
    )
    extensions: list[str] | None = Field(
        default=None,
        description="Restrict to these extensions (e.g. ['.py', '.md']). None = all.",
    )
    max_file_size_bytes: int = Field(
        default=5_242_880,
        ge=1,
        description="Skip files larger than this.",
    )

    @field_validator("root_paths")
    @classmethod
    def _root_paths_must_be_absolute(cls, v: list[str]) -> list[str]:
        for p in v:
            if not p.startswith("/"):
                msg = f"root_path must be absolute: {p!r}"
                raise ValueError(msg)
        return v


__all__ = ["ModelCrawlFilesystemRequest"]
