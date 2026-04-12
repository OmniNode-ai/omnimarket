# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Task contract models for dispatch evidence and DoD verification.

Rev 7 (C6 fix): check_command str replaced with typed EnumDodCheckType.
Each value dispatches to a hardcoded function in dod_verification_registry.
No shell commands are constructed from ticket text.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel


class EnumDodCheckType(StrEnum):
    """Closed enum of DoD verification check types.

    Each value maps to a hardcoded function in dod_verification_registry.py.
    No arbitrary commands — all function parameters come from ModelTaskContract fields.
    """

    PR_OPENED = "pr_opened"
    TESTS_PASS = "tests_pass"
    GOLDEN_CHAIN = "golden_chain"
    PRE_COMMIT_CLEAN = "pre_commit_clean"
    RENDERED_OUTPUT = "rendered_output"
    OVERSEER_5CHECK = "overseer_5check"


class ModelDodEvidenceCheck(BaseModel, frozen=True, extra="forbid"):
    """A single DoD evidence check declaration."""

    check_type: EnumDodCheckType
    required: bool = True
    timeout_seconds: int = 30


class ModelTaskContract(BaseModel, frozen=True, extra="forbid"):
    """Contract for a dispatched task — tracks identity, DoD checks, and dispatch metadata."""

    task_id: str
    ticket_id: str
    target_repo: str
    target_branch_pattern: str
    dod_evidence: list[ModelDodEvidenceCheck]
    dispatched_at: datetime
    dispatch_path: str  # "dogfood" | "agent_bypass"
    model_used: str  # "sonnet" | "qwen3-coder" | "deepseek-r1"
    stall_timeout_seconds: int | None = None


__all__: list[str] = [
    "EnumDodCheckType",
    "ModelDodEvidenceCheck",
    "ModelTaskContract",
]
