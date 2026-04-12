# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Task contract models for session bootstrap Rev 7.

EnumDodCheckType replaces the old free-text check_command field, eliminating
command injection via Linear ticket text (C6 fix from hostile review).

All verification functions are keyed by enum value in dod_verification_registry.py
— no arbitrary strings are executed as shell commands.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumDodCheckType(StrEnum):
    """Closed enum of allowed Definition-of-Done check types.

    Each value maps to a hardcoded verification function in
    dod_verification_registry.py.  No shell commands are constructed from
    Linear ticket text or any other external input (C6 fix).
    """

    PR_OPENED = "pr_opened"
    TESTS_PASS = "tests_pass"
    GOLDEN_CHAIN = "golden_chain"
    PRE_COMMIT_CLEAN = "pre_commit_clean"
    RENDERED_OUTPUT = "rendered_output"
    OVERSEER_5CHECK = "overseer_5check"


class ModelDodEvidenceCheck(BaseModel):
    """A single DoD evidence check linked to a hardcoded verification function."""

    model_config = ConfigDict(extra="forbid")

    check_type: EnumDodCheckType
    required: bool = True
    timeout_seconds: int = 30


class ModelTaskContract(BaseModel):
    """Per-ticket contract written by build_dispatch_pulse when a worker is dispatched.

    Persisted to .onex_state/task-contracts/{task_id}.json.
    Read by CronOutputVerificationRoutine post-tick to verify work actually finished.
    """

    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(description="Internal task ID, e.g. 'build-8505'")
    ticket_id: str = Field(description="Linear ticket ID, e.g. 'OMN-8505'")
    target_repo: str
    target_branch_pattern: str = Field(
        description="Branch glob, e.g. 'jonah/omn-8505-*'"
    )
    dod_evidence: list[ModelDodEvidenceCheck]
    dispatched_at: datetime
    dispatch_path: str = Field(description="'dogfood' | 'agent_bypass'")
    model_used: str = Field(description="'sonnet' | 'qwen3-coder' | 'deepseek-r1'")
    stall_timeout_seconds: int | None = Field(
        default=None,
        description="Override derived stall threshold for long-running tasks",
    )


__all__: list[str] = [
    "EnumDodCheckType",
    "ModelDodEvidenceCheck",
    "ModelTaskContract",
]
