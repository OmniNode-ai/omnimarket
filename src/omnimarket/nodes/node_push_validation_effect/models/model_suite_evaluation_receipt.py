# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelSuiteEvaluationReceipt — the fixed-commit suite-evaluation outcome
receipt (OMN-16524, rung R1, plan §3.3e.2b's attestation field set).

The handler's return value and the payload of BOTH terminal topics
(`onex.evt.omnimarket.suite-evaluation-completed.v1` for a run that reached
a verdict — pass OR fail; `onex.evt.omnimarket.suite-evaluation-failed.v1`
is reserved for infrastructure/handler errors, same split as
`node_push_validation_effect`'s COMPLETED/FAILED convention — a red suite is
a SUCCESSFUL node execution, never routed to the failure topic).

Field-for-field, this is plan §3.3e.2b's minimum attestation set:

* `evaluated_tree_digest` — "what was actually run." A 40-hex git tree SHA,
  computed by the CLIENT via `git rev-parse <commit_sha>^{tree}` AFTER
  checkout — independently, never copied from the caller's request. R1's own
  acceptance criterion is that this matches an independently-computed
  `git rev-parse HEAD^{tree}` for the fixed commit.
* `selector_policy_digest` — a sha256 hex digest binding "by what rules the
  suite scope was chosen." For R1 (plan §3.3e.5, open question 5: derivation
  is explicitly NOT settled by this rung) this is computed over the resolved
  suite invocation (the `suite_scope` string) plus the evaluated commit's own
  `pyproject.toml` bytes when present — a real, independently-recomputable
  digest, not a placeholder constant. Which files constitute "the policy" in
  full generality is left open for a later rung, exactly as the plan records.
* `suite_scope` — the exact pytest target executed; echoed so a scoped run
  can never be misread as a full-suite verdict.
* `verdict` — pass/fail. Suite failure is a SUCCESSFUL node execution.

`host_identity` mirrors `node_push_validation_effect`'s separate-field
convention (machine identity, never conflated with a credential — R1 has no
credential at all, since it never touches git remotes).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    field_validator,
    model_validator,
)


class EnumSuiteEvaluationVerdict(StrEnum):
    """Terminal verdict for one fixed-commit suite evaluation."""

    PASS = "pass"
    FAIL = "fail"


class ModelSuiteEvaluationReceipt(BaseModel):
    """Content-addressed attestation receipt for one suite-evaluation run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(..., description="Copied from request.correlation_id.")
    tenant_principal_id: str = Field(
        ...,
        pattern=r"^t-[0-9a-f]{32}$",
        description="Echo of the immutable request principal.",
    )
    requester: str = Field(..., min_length=1, description="Requester echo.")
    repo: str = Field(..., description="Repo slug echo.")
    commit_sha: str = Field(
        ...,
        pattern=r"^[0-9a-f]{40}$",
        description="Echo of the requested fixed commit (the CALLER's claim).",
    )
    evaluated_tree_digest: str = Field(
        ...,
        pattern=r"^[0-9a-f]{40}$",
        description="git tree SHA of commit_sha, computed by the executor "
        "AFTER checkout via `git rev-parse <sha>^{tree}` — independently "
        "verifiable, never caller-asserted. R1's content-addressing AC.",
    )
    selector_policy_digest: str = Field(
        ...,
        pattern=r"^[0-9a-f]{64}$",
        description="sha256 hex over the resolved suite_scope invocation and "
        "the evaluated commit's pyproject.toml bytes (when present) — a "
        "real, independently-recomputable digest of what determined this "
        "run's scope (plan §3.3e.5 open question 5: full derivation is NOT "
        "settled by R1).",
    )
    suite_scope: str = Field(
        ..., min_length=1, description="The exact pytest target executed."
    )
    verdict: EnumSuiteEvaluationVerdict = Field(
        ..., description="Suite verdict; suite failure is a SUCCESSFUL execution."
    )
    suite_log_digest: str = Field(
        ...,
        pattern=r"^[0-9a-f]{64}$",
        description="sha256 hex of the COMPLETE suite log — always present "
        "(R1 has no abort path; the suite always runs).",
    )
    host_identity: str = Field(
        ...,
        min_length=1,
        description="Machine identity that ran the suite (e.g. "
        "'gate-runner-201', the container's stable hostname).",
    )
    failure_detail: str | None = Field(
        default=None,
        description="Pytest tail on verdict=fail; None on a clean pass.",
    )
    started_at: str = Field(..., description="ISO-8601 UTC Z start timestamp.")
    completed_at: str = Field(..., description="ISO-8601 UTC Z end timestamp.")

    @field_validator("correlation_id")
    @classmethod
    def _correlation_id_is_uuid(cls, value: str) -> str:
        UUID(value)  # raises ValueError on non-UUID input
        return value

    @field_validator("started_at", "completed_at")
    @classmethod
    def _timestamps_are_utc_z(cls, value: str) -> str:
        if not value.endswith("Z"):
            raise ValueError("timestamps must be ISO-8601 UTC with a 'Z' suffix")
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return value

    @model_validator(mode="after")
    def _enforce_verdict_invariants(self) -> ModelSuiteEvaluationReceipt:
        if (
            self.verdict is EnumSuiteEvaluationVerdict.FAIL
            and not (self.failure_detail or "").strip()
        ):
            raise ValueError(
                "verdict=fail requires a non-empty failure_detail — a red "
                "suite must record why"
            )
        if (
            self.verdict is EnumSuiteEvaluationVerdict.PASS
            and self.failure_detail is not None
        ):
            raise ValueError(
                "verdict=pass requires failure_detail=None — a clean pass "
                "carries no failure text"
            )
        return self

    @property
    def projection_key(self) -> tuple[str, str]:
        """Tenant-scoped key, mirroring node_push_validation_effect's convention."""
        return (self.tenant_principal_id, self.correlation_id)

    @computed_field(  # type: ignore[prop-decorator]
        description="completed_at - started_at, milliseconds."
    )
    @property
    def execution_duration_ms(self) -> int:
        started = datetime.fromisoformat(self.started_at.replace("Z", "+00:00"))
        completed = datetime.fromisoformat(self.completed_at.replace("Z", "+00:00"))
        return round((completed - started).total_seconds() * 1000)

    @computed_field(  # type: ignore[prop-decorator]
        description="sha256 hex over this receipt's own canonical JSON "
        "(every field except this one)."
    )
    @property
    def receipt_integrity_hash(self) -> str:
        canonical = self.model_dump(mode="json", exclude={"receipt_integrity_hash"})
        canonical_json = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


__all__ = ["EnumSuiteEvaluationVerdict", "ModelSuiteEvaluationReceipt"]
