# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models for OmniGate receipt verification."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelReceiptVerifierInput(BaseModel):
    """Input for verifying an inline PR-body OmniGate receipt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pr_body: str = Field(description="Pull request body containing the inline receipt.")
    repo_path: str = Field(description="Repository checkout path.")
    config_path: str = Field(description="Trusted base .omnigate.yaml path.")
    repository_id: str
    repository_url: str
    base_sha: str
    head_sha: str
    actor: str | None = None


class ModelReceiptVerifierResult(BaseModel):
    """Fixed decision result returned by the verifier node."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    action: str
    reason: str
    receipt_diff_hash: str | None = None
    checked_at: str


__all__ = ["ModelReceiptVerifierInput", "ModelReceiptVerifierResult"]
