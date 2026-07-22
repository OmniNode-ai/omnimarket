# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelPushValidationRequest — the push-validation command payload (OMN-14920).

Field-by-field match with the gateway wire payload built by omninode_infra's
onex-api for workflow_type ``push-validation`` (gateway P2 tenant #1). The
caller-facing gateway schema accepts exactly ``repo`` / ``branch`` /
``expected_head_sha`` / ``requester`` (``additionalProperties: false``); the
gateway then stamps ``correlation_id`` / ``emitted_at`` / ``tenant_id`` /
``tenant_principal_id`` into the wire payload in that insertion order.

Seam invariants (asserted by the committed cross-repo fixture tests):

* ``correlation_id`` MUST equal the envelope-level ``correlation_id`` — the
  gateway guarantees this; the receipt copies it from here, NEVER from a Kafka
  transport header (P1 found header/envelope divergence).
* ``tenant_principal_id`` is REQUIRED and non-blank (immutable ``t-<32hex>``,
  slug-independent by construction) — it is the tenant-scoped projection key.
  An absent/blank principal FAILS the request; optional-input-silent-skip is
  banned.
* ``tenant_id`` is the mutable tenant SLUG — attribution/logging only, never
  authorization.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ModelPushValidationRequest(BaseModel):
    """Command to run the hook-verified, suite-gated, fail-closed branch push."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(
        ...,
        pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$",
        max_length=140,
        description="Repo slug (owner/name), e.g. 'OmniNode-ai/omnibase_core'.",
    )
    branch: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Bare branch name to validate and push (no refs/heads/).",
    )
    expected_head_sha: str = Field(
        ...,
        pattern=r"^[0-9a-f]{40}$",
        description="Exact 40-lowercase-hex commit the branch head must be at; "
        "FAIL-CLOSED — any divergence aborts (outcome=stale_head), no silent "
        "refetch, no retry-with-new-head.",
    )
    requester: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="Human/session/agent handle for audit, "
        "e.g. 'session:fable-dogfood-0722'.",
    )
    correlation_id: str = Field(
        ...,
        description="UUID string; MUST equal the envelope-level correlation_id "
        "(gateway invariant — never read from a transport header).",
    )
    emitted_at: str = Field(
        ...,
        description="ISO-8601 UTC timestamp with Z suffix (gateway-stamped).",
    )
    tenant_id: str | None = Field(
        default=None,
        description="Tenant SLUG, attribution only — never authorization.",
    )
    tenant_principal_id: str = Field(
        ...,
        pattern=r"^t-[0-9a-f]{32}$",
        description="REQUIRED immutable principal ('t-' + tenant UUID hex); "
        "the tenant-scoped projection key. The handler FAILS the request if "
        "absent/blank.",
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


__all__ = ["ModelPushValidationRequest"]
