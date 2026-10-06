# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What to read: the repo root, the rule, and any explicit filenames a hook passed."""

from pydantic import BaseModel, ConfigDict, Field


class ModelContractProjectionGatherRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    root: str = Field(
        description="Repository root; every gathered path is relative to it."
    )
    rule: str = Field(description="One of access, dlq, cursor.")
    filenames: tuple[str, ...] = Field(
        default=(), description="dlq rule: explicit handler paths (pre-commit mode)."
    )
    baseline_path: str = Field(
        default="scripts/validation/projection_cursor_baseline.txt",
        description="cursor rule: frozen shrink-only baseline, relative to root.",
    )
