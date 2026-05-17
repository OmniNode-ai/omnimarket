# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models for OmniGate receipt generation."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelReceiptGeneratorInput(BaseModel):
    """Input for generating a canonical OmniGate receipt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    config_path: str = Field(description="Path to trusted .omnigate.yaml.")
    repo_path: str = Field(description="Repository root used for diff hashing.")
    repository_id: str = Field(description="GitHub repository id or authority id.")
    project_name: str | None = Field(default=None, description="Display project name.")
    project_url: str | None = Field(default=None, description="Repository URL.")
    base_sha: str
    head_sha: str
    commit_sha: str
    branch: str = Field(default="", description="Display branch metadata.")
    checks: tuple[dict[str, object], ...] = Field(default=())
    sign: bool = Field(
        default=True, description="Sign when trusted config requires Sigstore."
    )
    allow_empty_diff: bool = Field(default=False)


class ModelReceiptGeneratorResult(BaseModel):
    """Generated receipt payload."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    receipt: dict[str, object]
    receipt_json: str
    signed: bool
    diff_hash: str
    config_hash: str


__all__ = ["ModelReceiptGeneratorInput", "ModelReceiptGeneratorResult"]
