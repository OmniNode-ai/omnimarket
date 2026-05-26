# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request model for the verification sweep orchestrator."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelVerificationSweepOrchestratorRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    targets: list[str] = Field(
        default_factory=list,
        description="Ticket IDs to verify (e.g. ['OMN-5400', 'OMN-5401']).",
    )
    epic: str | None = Field(
        default=None,
        description="Epic ID — discover and verify all child tickets.",
    )
    check_types: list[str] = Field(
        default_factory=list,
        description=(
            "Verification phases to run: dashboard | database | dod_evidence. "
            "Empty = all phases."
        ),
    )
    dry_run: bool = Field(
        default=False,
        description="When true, print results without writing receipts or Linear comments.",
    )
    pr: str | None = Field(
        default=None,
        description="GitHub PR reference (owner/repo#number) for pre-merge mode.",
    )
    timeout_seconds: int = Field(
        default=30,
        description="Hard timeout (seconds) for a single pre-merge PR verification run.",
    )
