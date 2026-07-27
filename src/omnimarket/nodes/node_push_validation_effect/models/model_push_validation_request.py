# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelPushValidationRequest — the push-validation command payload (OMN-14920,
Contract v2 additive fields OMN-14976).

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

Contract v2 (OMN-14976) — SESSION-SCOPED DELIVERY, honestly bounded. Plan
source: docs/plans/2026-07-23-distributed-validation-context-aware-runtime-plan.md
§2 D1+D2. The full ticket specifies a coordinated two-repo lockstep landing:
"omnimarket accepts the new optional fields FIRST, gateway advertises them
SECOND." This revision ships exactly the FIRST half — omnimarket-side,
additive, backward-compatible fields only:

* ``mode``: ``validate_and_push`` (default, unchanged landing behavior) or
  ``validate_only`` (opt-in; suite runs, push is intentionally skipped).
* ``source_identity``: an OPTIONAL discriminated companion to
  ``expected_head_sha`` — ``commit`` | ``tree`` | ``commit+patch``. Because
  the gateway has not been updated this session (that is explicitly the
  SECOND leg, not built here), no real caller sends this field yet; when
  absent, behavior is byte-identical to pre-Contract-v2 (implicit ``commit``
  identity from ``expected_head_sha``). When present with ``identity_type
  != "commit"``, ``mode`` MUST be ``validate_only`` — "commit is the only
  member the push flow accepts" (ticket text) is enforced here as a model
  invariant, not left to the handler to remember.
* The byte-pinned cross-repo request FIXTURE
  (``fixtures/push_validation_requested_wire_envelope.json``) is
  DELIBERATELY UNCHANGED — it represents what the (not-yet-updated) gateway
  actually sends today, and both new fields default such that parsing it
  produces byte-identical semantics to pre-Contract-v2.
* NOT in this revision (named, explicit residuals): the omninode_infra
  gateway-side advertisement of these fields; the laptop tenant credential
  provisioning; the bundle-transfer source-identity dereference (OMN-14979,
  blocked-by this ticket); the pre-push client (OMN-14980).
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class EnumPushValidationMode(StrEnum):
    """Request mode — validate-and-push (default, unchanged) or validate-only."""

    VALIDATE_AND_PUSH = "validate_and_push"
    VALIDATE_ONLY = "validate_only"


class EnumSourceIdentityType(StrEnum):
    """Discriminator for ModelSourceIdentity members."""

    COMMIT = "commit"
    TREE = "tree"
    COMMIT_PATCH = "commit+patch"


class ModelSourceIdentityCommit(BaseModel):
    """A pushed/pushable commit — the only member the push flow accepts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    identity_type: Literal[EnumSourceIdentityType.COMMIT] = (
        EnumSourceIdentityType.COMMIT
    )
    expected_head_sha: str = Field(
        ...,
        pattern=r"^[0-9a-f]{40}$",
        description="Same value as the top-level expected_head_sha — a model "
        "validator enforces they match when both are present.",
    )


class ModelSourceIdentityTree(BaseModel):
    """An uncommitted working-tree state at a known base commit.

    ``tree_hash`` distinguishes two dirty trees at the same branch+head — a
    receipt for tree A must never authorize tree B (plan invariant #1). Not
    accepted by the push flow (mode must be validate_only).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    identity_type: Literal[EnumSourceIdentityType.TREE] = EnumSourceIdentityType.TREE
    expected_head_sha: str = Field(
        ..., pattern=r"^[0-9a-f]{40}$", description="Base commit the tree sits on."
    )
    tree_hash: str = Field(
        ...,
        pattern=r"^[0-9a-f]{64}$",
        description="sha256 hex of the working-tree state (content, not a git "
        "commit) — the distinguishing value for two dirty trees at the same "
        "branch+head.",
    )


class ModelSourceIdentityCommitPatch(BaseModel):
    """A committed base plus an uncommitted patch on top.

    Not accepted by the push flow (mode must be validate_only).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    identity_type: Literal[EnumSourceIdentityType.COMMIT_PATCH] = (
        EnumSourceIdentityType.COMMIT_PATCH
    )
    expected_head_sha: str = Field(
        ...,
        pattern=r"^[0-9a-f]{40}$",
        description="Committed base the patch applies to.",
    )
    patch_hash: str = Field(
        ...,
        pattern=r"^[0-9a-f]{64}$",
        description="sha256 hex of the patch content — the distinguishing "
        "value for two different patches on the same base.",
    )


ModelSourceIdentity = Annotated[
    ModelSourceIdentityCommit
    | ModelSourceIdentityTree
    | ModelSourceIdentityCommitPatch,
    Field(discriminator="identity_type"),
]


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
    mode: EnumPushValidationMode = Field(
        default=EnumPushValidationMode.VALIDATE_AND_PUSH,
        description="Contract v2 (OMN-14976): validate_and_push (default, "
        "landing behavior unchanged) or validate_only (suite runs, push is "
        "intentionally skipped, outcome=validated).",
    )
    source_identity: ModelSourceIdentity | None = Field(
        default=None,
        description="Contract v2 (OMN-14976): OPTIONAL discriminated companion "
        "to expected_head_sha. Absent (the only case any real caller produces "
        "this session — gateway advertisement is a separate, not-yet-built "
        "leg) means implicit commit identity from expected_head_sha, "
        "byte-identical to pre-Contract-v2 behavior.",
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

    @model_validator(mode="after")
    def _source_identity_invariants(self) -> ModelPushValidationRequest:
        if self.source_identity is None:
            return self

        if (
            self.source_identity.identity_type == EnumSourceIdentityType.COMMIT
            and self.source_identity.expected_head_sha != self.expected_head_sha
        ):
            raise ValueError(
                "source_identity.expected_head_sha "
                f"({self.source_identity.expected_head_sha!r}) must equal the "
                f"top-level expected_head_sha ({self.expected_head_sha!r}) "
                "when identity_type=commit"
            )

        if (
            self.mode == EnumPushValidationMode.VALIDATE_AND_PUSH
            and self.source_identity.identity_type != EnumSourceIdentityType.COMMIT
        ):
            raise ValueError(
                "mode=validate_and_push only accepts source_identity="
                f"commit (or absent) — got identity_type="
                f"{self.source_identity.identity_type.value!r}; the push flow "
                "never pushes an uncommitted tree/patch. Use "
                "mode=validate_only for tree/commit+patch identities."
            )

        return self


__all__ = [
    "EnumPushValidationMode",
    "EnumSourceIdentityType",
    "ModelPushValidationRequest",
    "ModelSourceIdentity",
    "ModelSourceIdentityCommit",
    "ModelSourceIdentityCommitPatch",
    "ModelSourceIdentityTree",
]
