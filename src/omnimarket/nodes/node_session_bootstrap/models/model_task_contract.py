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


class EnumEvidenceArtifactKind(StrEnum):
    """Classification of the artifact backing a RENDERED_OUTPUT evidence check.

    Fed by ``classify_evidence_kind`` in dod_verification_registry.py
    (OMN-13776). Distinguishes an HTTP-probe artifact (curl/httpx/wget against
    a non-UI, non-browser-rendered endpoint) from an artifact that actually
    requires browser/UI rendering proof — so a probe against ``/ready`` or a
    projection API no longer wrongly demands a Playwright receipt, while a
    dashboard/UI-class receipt still does (no regression of OMN-13024 /
    OMN-13052).
    """

    HTTP_EVIDENCE = "http_evidence"
    UI_RENDERED = "ui_rendered"
    UNKNOWN = "unknown"


class ModelDodEvidenceCheck(BaseModel):
    """A single DoD evidence check linked to a hardcoded verification function."""

    model_config = ConfigDict(extra="forbid")

    check_type: EnumDodCheckType
    required: bool = True
    timeout_seconds: int = 30
    artifact_command: str | None = Field(
        default=None,
        description=(
            "Command/tool used to produce the evidence artifact, e.g. "
            "'curl -sf https://host/ready'. Only meaningful for "
            "RENDERED_OUTPUT checks; used by classify_evidence_kind to tell "
            "an HTTP probe apart from a browser-rendered UI check."
        ),
    )
    target_endpoint: str | None = Field(
        default=None,
        description=(
            "Endpoint/path the artifact_command targeted, e.g. '/ready' or "
            "a dashboard host root. Only meaningful for RENDERED_OUTPUT "
            "checks."
        ),
    )


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
    "EnumEvidenceArtifactKind",
    "ModelDodEvidenceCheck",
    "ModelTaskContract",
]
