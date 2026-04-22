# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models for node_verification_receipt_generator."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ModelVerificationReceiptRequest(BaseModel):
    """Input: a task-completed claim with PR references to verify."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str = Field(description="Task identifier (e.g. 'OMN-9403').")
    claim: str = Field(
        description="What the task claims to have done (e.g. 'all tests pass').",
    )
    repo: str = Field(
        default="",
        description="GitHub repo slug for PR verification.",
    )
    pr_number: int | None = Field(
        default=None,
        description="PR number to verify CI checks for.",
    )
    worktree_path: str = Field(
        default="",
        description="Path to the worktree for pytest verification.",
    )
    verify_ci: bool = Field(
        default=True,
        description="Whether to verify CI checks via gh.",
    )
    verify_tests: bool = Field(
        default=True,
        description="Whether to run pytest and capture exit code.",
    )
    dry_run: bool = Field(
        default=False,
        description="Return receipt without running verification.",
    )


class ModelFileTestResult(BaseModel):
    """Per-file pytest result."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    file: str = Field(description="Test file path relative to worktree root.")
    passed: int = Field(description="Number of passing tests in this file.")
    failed: int = Field(description="Number of failing tests in this file.")
    errors: int = Field(default=0, description="Number of errors in this file.")
    skipped: int = Field(default=0, description="Number of skipped tests in this file.")
    exit_code: int = Field(description="0 if all passed, 1 if any failed/errored.")


class ModelCheckEvidence(BaseModel):
    """Evidence from a single verification dimension."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dimension: str = Field(description="Check dimension (e.g. 'ci_checks', 'pytest').")
    passed: bool
    summary: str = Field(default="", description="Human-readable summary.")
    details: dict[str, str] = Field(
        default_factory=dict,
        description="Structured check details (e.g. check name -> conclusion).",
    )
    file_results: list[ModelFileTestResult] = Field(
        default_factory=list,
        description="Per-file pytest results (empty for non-pytest dimensions).",
    )


class ModelVerificationReceipt(BaseModel):
    """Output: verified evidence receipt for a task-completed claim."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str
    claim: str
    overall_pass: bool = Field(description="True only if ALL checks passed.")
    checks: list[ModelCheckEvidence] = Field(
        default_factory=list,
        description="Evidence from each verification dimension.",
    )
    verified_at: datetime
    verifier: str = Field(
        default="node_verification_receipt_generator",
        description="Node that produced this receipt.",
    )
