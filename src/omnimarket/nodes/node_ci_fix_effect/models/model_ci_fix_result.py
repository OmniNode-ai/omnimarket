# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Completion event for node_ci_fix_effect [OMN-8993]."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict


class CiFixResult(BaseModel):
    """Emitted when a CI fix has been attempted on a PR."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pr_number: int
    repo: str
    run_id_github: str
    failing_job_name: str
    correlation_id: UUID
    patch_applied: bool
    local_tests_passed: bool
    is_noop: bool
    error: str | None = None
    elapsed_seconds: float = 0.0
