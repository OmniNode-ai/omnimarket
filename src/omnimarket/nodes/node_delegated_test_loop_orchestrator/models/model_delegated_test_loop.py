# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request, result and seam models of the delegated test loop (OMN-19362).

The seam models (what the loop asks a child for, and what comes back) are
declared here, in the orchestrator's own package. The children's own models
are never imported: a runner maps one onto the other at the seam.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: Plan section 5: one WRITE plus up to two REPAIRs.
MAX_DELEGATE_CALLS = 3
#: The serialized result stays under this many bytes.
MAX_RESULT_BYTES = 4096

RunOutcome = Literal[
    "passed",
    "failed_call",
    "failed_collection",
    "error_setup",
    "no_tests",
    "infra_error",
]


class EnumLoopStatus(StrEnum):
    ACCEPTED_CALL = "accepted_call"
    ACCEPTED_MUTATION = "accepted_mutation"
    ACCEPTED_COLLECTION = "accepted_collection"
    CONTROL_DID_NOT_FAIL = "control_did_not_fail"
    FAILED = "failed"
    NO_PROGRESS = "no_progress"
    HOST_BUSY = "host_busy"
    INFRA_ERROR = "infra_error"


#: Operator ruling 2026-09-23T21:52:08Z decision (3).
HEADLINE_STATUSES = frozenset(
    {EnumLoopStatus.ACCEPTED_CALL, EnumLoopStatus.ACCEPTED_MUTATION}
)


class ModelLoopMutation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    find: str = Field(..., min_length=1)
    replace: str


class ModelDelegatedTestLoopRequest(BaseModel):
    """One criterion, two refs, and the rails the loop must respect."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    repo: str = Field(..., pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    fixed_ref: str = Field(..., pattern=r"^[0-9a-f]{40}$")
    prefix_ref: str = Field(..., pattern=r"^[0-9a-f]{40}$")
    criterion: str = Field(..., min_length=1, max_length=4000)
    target_path: str = Field(..., min_length=1)
    target_line_ranges: tuple[tuple[int, int], ...] = Field(
        default=(),
        description="1-based inclusive line ranges of the target shown to the "
        "model. Empty shows the whole file (the prompt compute caps it).",
    )
    test_path: str = Field(..., pattern=r"^tests/[A-Za-z0-9_./-]+\.py$")
    hide_paths: tuple[str, ...] = ()
    forbidden_fragments: tuple[str, ...] = ()
    mutations: tuple[ModelLoopMutation, ...] = ()
    timeout_seconds: int = Field(default=300, ge=1, le=600)
    max_delegate_calls: int = Field(
        default=MAX_DELEGATE_CALLS, ge=1, le=MAX_DELEGATE_CALLS
    )
    run_code_gates: bool = Field(
        default=True,
        description="OMN-19527: run the repository's lint and type gates over "
        "the written test with each fixed-ref run; findings on a passing test "
        "buy exactly one repair call that carries the gate digest verbatim.",
    )

    @field_validator("correlation_id")
    @classmethod
    def _uuid(cls, value: str) -> str:
        UUID(value)
        return value

    @property
    def is_negative_control(self) -> bool:
        return self.prefix_ref == self.fixed_ref


class ModelDelegateReply(BaseModel):
    """What one WRITE or REPAIR call returned."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    ok: bool = Field(..., description="The call completed and returned text.")
    test_source: str = ""
    invalid_reason: str = Field(
        default="", description="Why the reply is not a usable test, if it is not."
    )
    tokens_in: int = 0
    tokens_out: int = 0
    model: str = ""


class ModelRunDigest(BaseModel):
    """One focused run, as the failure digest classified it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    receipt_id: str
    receipt_status: Literal["completed", "host_busy", "infra_error"]
    outcome: RunOutcome
    exception_type: str = ""
    message: str = Field(default="", max_length=500)
    frames: str = Field(default="", max_length=1500)
    top_frame: str = ""
    failing_node_id: str = ""
    fingerprint: str = ""


class ModelGateDigestSeam(BaseModel):
    """What the repository gates said about the written test (OMN-19527)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    clean: bool
    infra_error: bool = False
    finding_count: int = Field(default=0, ge=0)
    digest_text: str = Field(default="", max_length=2000)
    fingerprint: str = ""


class ModelControlVerdict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: str
    headline: bool
    control_ref_role: Literal["prefix", "mutation"]
    control_outcome: str


class ModelLoopControl(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ref: str
    ref_role: Literal["prefix", "mutation"]
    outcome: str
    prefix_outcome: str


class ModelFinalDigest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: str
    exception_type: str = ""
    message: str = Field(default="", max_length=300)
    top_frame: str = ""
    fingerprint: str = ""


class ModelDelegatedTestLoopResult(BaseModel):
    """The compact result the Claude lane reads. No test source, no logs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    loop_run_id: str
    status: EnumLoopStatus
    headline: bool
    test_path: str
    test_source_sha256: str = ""
    attempts: int = Field(..., ge=0, le=MAX_DELEGATE_CALLS)
    control: ModelLoopControl | None = None
    delegate_run_ids: tuple[str, ...] = ()
    run_receipt_ids: tuple[str, ...] = ()
    final_digest: ModelFinalDigest | None = None
    local_tokens_in: int = 0
    local_tokens_out: int = 0
    wall_ms: int = 0
    detail: str = Field(default="", max_length=300)
    gate_clean: bool | None = Field(
        default=None,
        description="OMN-19527: whether the repository gates accepted the "
        "test the loop kept; None when no gate ran.",
    )
    gate_findings: int = Field(default=0, ge=0)
    gate_repairs: int = Field(default=0, ge=0, le=1)


__all__ = [
    "HEADLINE_STATUSES",
    "MAX_DELEGATE_CALLS",
    "MAX_RESULT_BYTES",
    "EnumLoopStatus",
    "ModelControlVerdict",
    "ModelDelegateReply",
    "ModelDelegatedTestLoopRequest",
    "ModelDelegatedTestLoopResult",
    "ModelFinalDigest",
    "ModelGateDigestSeam",
    "ModelLoopControl",
    "ModelLoopMutation",
    "ModelRunDigest",
    "RunOutcome",
]
