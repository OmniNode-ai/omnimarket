# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Task contract models for session bootstrap DoD verification.

EnumDodCheckType is a closed enum — each value maps to a hardcoded function
in dod_verification_registry.py. No arbitrary shell commands. (C6 fix)
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel


class EnumDodCheckType(StrEnum):
    PR_OPENED = "pr_opened"
    TESTS_PASS = "tests_pass"
    GOLDEN_CHAIN = "golden_chain"
    PRE_COMMIT_CLEAN = "pre_commit_clean"
    RENDERED_OUTPUT = "rendered_output"
    OVERSEER_5CHECK = "overseer_5check"


class ModelDodEvidenceCheck(BaseModel):
    check_type: EnumDodCheckType
    required: bool = True
    timeout_seconds: int = 30


class ModelTaskContract(BaseModel):
    task_id: str
    ticket_id: str
    target_repo: str
    target_branch_pattern: str
    dod_evidence: list[ModelDodEvidenceCheck]
    dispatched_at: datetime
    dispatch_path: str  # "dogfood" | "agent_bypass"
    model_used: str
    stall_timeout_seconds: int | None = None


__all__: list[str] = [
    "EnumDodCheckType",
    "ModelDodEvidenceCheck",
    "ModelTaskContract",
]
