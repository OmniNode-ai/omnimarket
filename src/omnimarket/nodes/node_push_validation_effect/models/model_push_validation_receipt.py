# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelPushValidationReceipt — the push-validation outcome receipt (OMN-14920).

The handler's return value and the payload of the COMPLETED terminal event
(``onex.evt.omnimarket.push-validation-completed.v1``). The receipt binds the
outcome to the pushed SHA and encodes the OMN-14920 acceptance criteria as
fail-closed model invariants:

* ``outcome=pushed`` requires a non-empty ``hook_id_readback`` (captured from
  the INSTALLED pre-push hook BEFORE any push attempt), ``push_exit == 0``, a
  green ``suite_verdict``, and ``remote_sha_readback == expected_head_sha``
  (post-push ``git ls-remote`` readback) — the receipt binds outcome to the
  pushed SHA.
* A red suite NEVER pushes: ``outcome=suite_failed`` forces
  ``push_exit=None`` (push not attempted) and ``suite_verdict=fail``. Suite
  failure is a SUCCESSFUL node execution emitted on the COMPLETED topic; the
  failure terminal topic is reserved for infrastructure/handler errors.
* ``expected_head_sha`` is FAIL-CLOSED: ``outcome=stale_head`` (observed
  remote head diverged) aborts before suite and push
  (``suite_verdict=not_run``, ``push_exit=None``); ``remote_sha_readback``
  carries the OBSERVED divergent head.
* ``already_pushed`` / ``refused`` are aborts: ``suite_verdict=not_run``,
  ``push_exit=None``. ``refused`` covers protected branches (dev/main) and
  unhooked clones — zero bypass flags (never ``--no-verify``) anywhere in the
  path.

Projection seam (omnimarket owns the projector): receipts project to
``projection_push_validation_receipt`` with PRIMARY KEY
``(tenant_principal_id, correlation_id)`` — one row per request, tenant-scoped
by the IMMUTABLE principal, never the mutable slug. ``correlation_id`` is
copied from the request payload, which the gateway guarantees equals the
ENVELOPE correlation_id — NEVER read from a Kafka transport header; the
projection consumer must assert payload.correlation_id ==
envelope.correlation_id and DLQ on divergence.

``host_identity`` (machine that ran suite+push) and ``credential_identity``
(mechanism-prefixed pushing credential, runtime readback) are SEPARATE fields
so the later durable-machine-credential swap changes ``credential_identity``
only and does not invalidate host-bound replay evidence.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class EnumPushValidationOutcome(StrEnum):
    """Terminal outcome discriminator for a push-validation run."""

    PUSHED = "pushed"
    ALREADY_PUSHED = "already_pushed"
    SUITE_FAILED = "suite_failed"
    STALE_HEAD = "stale_head"
    PUSH_FAILED = "push_failed"
    REFUSED = "refused"


class EnumSuiteVerdict(StrEnum):
    """Suite verdict; ``not_run`` for stale_head/already_pushed/refused aborts."""

    PASS = "pass"
    FAIL = "fail"
    NOT_RUN = "not_run"


_ABORT_OUTCOMES = frozenset(
    {
        EnumPushValidationOutcome.ALREADY_PUSHED,
        EnumPushValidationOutcome.STALE_HEAD,
        EnumPushValidationOutcome.REFUSED,
    }
)


class ModelPushValidationReceipt(BaseModel):
    """Fail-closed outcome receipt for one push-validation request."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: EnumPushValidationOutcome = Field(
        ..., description="Terminal outcome discriminator."
    )
    correlation_id: str = Field(
        ...,
        description="Copied from request.correlation_id (== ENVELOPE "
        "correlation_id, gateway invariant). Never read from a transport "
        "header. Projection key component.",
    )
    tenant_principal_id: str = Field(
        ...,
        pattern=r"^t-[0-9a-f]{32}$",
        description="Echo of the immutable request principal; projection key.",
    )
    tenant_id: str | None = Field(
        default=None, description="Tenant slug echo, attribution only."
    )
    requester: str = Field(..., min_length=1, description="Requester echo.")
    repo: str = Field(..., description="Repo slug echo.")
    branch: str = Field(..., min_length=1, description="Branch echo.")
    expected_head_sha: str = Field(
        ...,
        pattern=r"^[0-9a-f]{40}$",
        description="Echo of the fail-closed expected head SHA.",
    )
    hook_id_readback: str = Field(
        ...,
        description="Digest of the INSTALLED pre-push hook content in the "
        "working clone, captured BEFORE any push attempt (canary laptop-match "
        "value starts 138fd403…). Required non-empty for outcome=pushed; an "
        "unhooked clone refuses to push (outcome=refused).",
    )
    suite_verdict: EnumSuiteVerdict = Field(
        ...,
        description="Suite verdict; not_run for stale_head/already_pushed/"
        "refused aborts.",
    )
    suite_log_digest: str | None = Field(
        default=None,
        description="sha256 hex of the complete suite log file; None only "
        "when suite_verdict=not_run.",
    )
    push_exit: int | None = Field(
        default=None,
        description="git push process exit code; None whenever push was not attempted.",
    )
    remote_sha_readback: str | None = Field(
        default=None,
        description="`git ls-remote origin refs/heads/<branch>` AFTER push "
        "for outcome=pushed (must equal expected_head_sha); for stale_head "
        "the OBSERVED divergent remote head; None if unreachable.",
    )
    host_identity: str = Field(
        ...,
        min_length=1,
        description="Machine identity that ran suite+push (hostname readback, "
        "e.g. 'omninode-pc'). SEPARATE field from the credential.",
    )
    credential_identity: str = Field(
        ...,
        min_length=1,
        description="The pushing credential, mechanism-prefixed, read back at "
        "runtime (e.g. 'gh:jonahgabriel' via `gh api user --jq .login`).",
    )
    failure_detail: str | None = Field(
        default=None,
        description="Honest terminal detail (stale_expected_head_sha, "
        "protected_branch_refused, suite failure summary, push stderr tail); "
        "None on clean push.",
    )
    started_at: str = Field(..., description="ISO-8601 UTC Z start timestamp.")
    completed_at: str = Field(..., description="ISO-8601 UTC Z end timestamp.")

    @property
    def projection_key(self) -> tuple[str, str]:
        """PRIMARY KEY of projection_push_validation_receipt (tenant-scoped)."""
        return (self.tenant_principal_id, self.correlation_id)

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
    def _enforce_outcome_invariants(self) -> ModelPushValidationReceipt:
        if self.outcome is EnumPushValidationOutcome.PUSHED:
            if not self.hook_id_readback.strip():
                raise ValueError(
                    "outcome=pushed requires a non-empty hook_id_readback "
                    "(hook ID is captured BEFORE any push; an unhooked clone "
                    "refuses to push)"
                )
            if self.suite_verdict is not EnumSuiteVerdict.PASS:
                raise ValueError("outcome=pushed requires suite_verdict=pass")
            if self.push_exit != 0:
                raise ValueError("outcome=pushed requires push_exit == 0")
            if self.remote_sha_readback != self.expected_head_sha:
                raise ValueError(
                    "outcome=pushed requires remote_sha_readback == "
                    "expected_head_sha (the receipt binds outcome to the "
                    "pushed SHA)"
                )
        elif self.outcome is EnumPushValidationOutcome.SUITE_FAILED:
            if self.suite_verdict is not EnumSuiteVerdict.FAIL:
                raise ValueError("outcome=suite_failed requires suite_verdict=fail")
            if self.push_exit is not None:
                raise ValueError(
                    "outcome=suite_failed requires push_exit=None — a red "
                    "suite NEVER pushes"
                )
        elif self.outcome in _ABORT_OUTCOMES:
            if self.suite_verdict is not EnumSuiteVerdict.NOT_RUN:
                raise ValueError(
                    f"outcome={self.outcome.value} is an abort and requires "
                    "suite_verdict=not_run"
                )
            if self.push_exit is not None:
                raise ValueError(
                    f"outcome={self.outcome.value} is an abort and requires "
                    "push_exit=None (push was not attempted)"
                )
        elif self.outcome is EnumPushValidationOutcome.PUSH_FAILED:
            if self.suite_verdict is not EnumSuiteVerdict.PASS:
                raise ValueError(
                    "outcome=push_failed implies the suite passed before the "
                    "push was attempted (a red suite never reaches push)"
                )
            if self.push_exit is None or self.push_exit == 0:
                raise ValueError(
                    "outcome=push_failed requires a non-zero push_exit (the "
                    "push process ran and failed)"
                )

        # suite_log_digest is None iff the suite did not run.
        if (self.suite_log_digest is None) != (
            self.suite_verdict is EnumSuiteVerdict.NOT_RUN
        ):
            raise ValueError(
                "suite_log_digest must be None exactly when "
                "suite_verdict=not_run (a run suite always has a complete "
                "log digest)"
            )
        return self


__all__ = [
    "EnumPushValidationOutcome",
    "EnumSuiteVerdict",
    "ModelPushValidationReceipt",
]
