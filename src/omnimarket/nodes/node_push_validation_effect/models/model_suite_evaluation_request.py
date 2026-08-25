# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelSuiteEvaluationRequest — the fixed-commit suite-evaluation command
payload (OMN-16524, rung R1 of the Architecture-3 suite-execution ladder,
plan `docs/plans/2026-08-23-cloud-ci-offload-plan.md` §3.3e).

Extends `node_push_validation_effect` (OMN-14920) with a SECOND operation
(`run_suite_evaluation`) rather than reusing `run_push_validation`'s request
shape. The two operations are deliberately NOT unified — see the module
docstring on `handler_suite_evaluation_effect.py` for the extend-vs-net-new
reasoning recorded for R1's mandatory prior-art check.

R1's scope is deliberately narrow (plan §3.3e.4 row R1): the caller names one
hardcoded, already-merged commit — never a branch to push, never a live
submission. There is no `expected_head_sha`-vs-branch-HEAD fail-closed check
here (that is a push-safety control this read-only operation has no need
for) and no `mode`/`source_identity` discriminated union (R1 has no bundle
leg and no push leg at all).
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

# R1's fixed default: the governed unit suite, unnarrowed. A caller MAY
# override with a different relative pytest target for this rung's own
# proof runs (e.g. a smaller subtree to keep the gate-runner container's
# bounded 4-CPU/8GB envelope from queuing behind a multi-hour full suite),
# but the default is the honest "unit suite" the ticket names.
DEFAULT_SUITE_SCOPE: str = "tests/unit"


class ModelSuiteEvaluationRequest(BaseModel):
    """Command to run one already-merged commit's suite in the bounded
    gate-runner container and emit a content-addressed attestation receipt.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(
        ...,
        pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$",
        max_length=140,
        description="Repo slug (owner/name), e.g. 'OmniNode-ai/omnibase_compat'.",
    )
    commit_sha: str = Field(
        ...,
        pattern=r"^[0-9a-f]{40}$",
        description="Exact 40-lowercase-hex ALREADY-MERGED commit to evaluate. "
        "Not a branch tip claim — the client resolves and checks this out "
        "directly (detached), independent of any branch's live head.",
    )
    suite_scope: str = Field(
        default=DEFAULT_SUITE_SCOPE,
        min_length=1,
        max_length=255,
        description="Relative pytest target executed inside the gate-runner "
        "container, e.g. 'tests/unit'. Echoed on the receipt so a scoped run "
        "can never be misread as a full-suite verdict (plan §3.3e.2b).",
    )
    requester: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="Human/session/agent handle for audit, "
        "e.g. 'session:omn16524-r1-build'.",
    )
    correlation_id: str = Field(
        ...,
        description="UUID string; MUST equal the envelope-level correlation_id "
        "(same seam invariant as node_push_validation_effect).",
    )
    emitted_at: str = Field(
        ...,
        description="ISO-8601 UTC timestamp with Z suffix (caller-stamped).",
    )
    tenant_principal_id: str = Field(
        ...,
        pattern=r"^t-[0-9a-f]{32}$",
        description="REQUIRED immutable principal ('t-' + tenant UUID hex); "
        "the tenant-scoped receipt key. The handler FAILS the request if "
        "absent/blank — optional-input-silent-skip is banned.",
    )

    @field_validator("correlation_id")
    @classmethod
    def _correlation_id_is_uuid(cls, value: str) -> str:
        UUID(value)  # raises ValueError on non-UUID input
        return value

    @field_validator("emitted_at")
    @classmethod
    def _emitted_at_is_utc_z(cls, value: str) -> str:
        if not value.endswith("Z"):
            raise ValueError("emitted_at must be ISO-8601 UTC with a 'Z' suffix")
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return value


__all__ = ["DEFAULT_SUITE_SCOPE", "ModelSuiteEvaluationRequest"]
