# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""RAM-aware host advertisement + worker admission models (D3, OMN-14977).

Encodes the headroom formula and admission decision spec'd in
``docs/plans/2026-07-23-distributed-validation-context-aware-runtime-plan.md``
§2 D3 (Rev 2, hostile-reviewed):

* Headroom formula on macOS: ``usable = total - wired - inference_reservation``,
  where ``wired`` is read from ``vm_stat`` pages (Metal-wired model weights are
  invisible to per-process RSS) and ``inference_reservation`` is a *declared*
  per-host overlay value, never inferred.
* Carrier: a heartbeat-cadenced advertisement; staleness bound is
  ``2 * cadence_seconds`` — a stale advertisement makes the host ineligible
  (fail-closed, never "assume still fresh").
* Admission point: the WORKER refuses a job above its memory policy (there is
  no scheduler yet); refusal is a typed receipt, never a silent queue.
* Mid-suite collapse: if headroom crosses the declared floor mid-run, the
  policy is finish-or-abort — never swap-thrash.

This node is pure COMPUTE (rule 7a): it consumes an already-parsed
advertisement (page counts * page size, already resolved to bytes by the
EFFECT that reads ``vm_stat``) and a request; it performs no I/O itself.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class EnumMemoryAdmissionOutcome(StrEnum):
    """Terminal outcome discriminator for one admission evaluation."""

    ADMITTED = "admitted"
    REFUSED = "refused"


class EnumMemoryAdmissionRefusalReason(StrEnum):
    """Typed refusal reason — never a free-text-only refusal."""

    STALE_ADVERTISEMENT = "stale_advertisement"
    INSUFFICIENT_HEADROOM = "insufficient_headroom"


class EnumMidRunCollapsePolicy(StrEnum):
    """Mid-suite collapse policy when headroom crosses the floor mid-run.

    Never ``swap_thrash`` — that option does not exist by construction; the
    only declarable policies restore the inference-latency-degradation pause
    guardrail (plan §2 D3).
    """

    FINISH = "finish"
    ABORT = "abort"


class ModelHostMemoryAdvertisement(BaseModel):
    """One heartbeat-cadenced memory-capacity advertisement from a host.

    ``wired_bytes`` and ``total_bytes`` are pre-resolved from ``vm_stat`` page
    counts (``pages * page_size``) by the advertising EFFECT — this model
    never parses raw ``vm_stat`` text, keeping the compute node pure.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    host_identity: str = Field(
        ..., min_length=1, description="Advertising host hostname readback."
    )
    total_bytes: int = Field(..., gt=0, description="Total physical RAM, bytes.")
    wired_bytes: int = Field(
        ...,
        ge=0,
        description="Wired pages * page_size from `vm_stat` on the "
        "advertising host — Metal-wired model weights are invisible to "
        "per-process RSS, so this must come from vm_stat, never /proc-style "
        "per-process accounting.",
    )
    inference_reservation_bytes: int = Field(
        ...,
        ge=0,
        description="Declared per-host overlay reservation for local "
        "inference workloads — a DECLARED value, never inferred from live "
        "usage (plan §2 D3).",
    )
    cadence_seconds: int = Field(
        ...,
        gt=0,
        description="Heartbeat cadence this advertisement is "
        "emitted on; staleness bound is 2x this value.",
    )
    collapse_policy: EnumMidRunCollapsePolicy = Field(
        ...,
        description="Declared mid-suite collapse policy for this host — "
        "finish-or-abort, never swap-thrash.",
    )
    advertised_at: str = Field(
        ..., description="ISO-8601 UTC Z timestamp this advertisement was emitted."
    )

    @field_validator("advertised_at")
    @classmethod
    def _advertised_at_is_utc_z(cls, value: str) -> str:
        if not value.endswith("Z"):
            raise ValueError("advertised_at must be ISO-8601 UTC with a 'Z' suffix")
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return value

    @property
    def usable_bytes(self) -> int:
        """``usable = total - wired - inference_reservation``, floored at 0."""
        raw = self.total_bytes - self.wired_bytes - self.inference_reservation_bytes
        return max(raw, 0)


class ModelMemoryAdmissionRequest(BaseModel):
    """A single admission evaluation: one job against one host's advertisement."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    advertisement: ModelHostMemoryAdvertisement = Field(
        ..., description="Most recent advertisement from the candidate host."
    )
    requested_job_memory_bytes: int = Field(
        ..., gt=0, description="Memory the incoming job declares it needs."
    )
    evaluated_at: str = Field(
        ...,
        description="ISO-8601 UTC Z timestamp of this admission evaluation "
        "(observation time — used against advertised_at for staleness).",
    )

    @field_validator("evaluated_at")
    @classmethod
    def _evaluated_at_is_utc_z(cls, value: str) -> str:
        if not value.endswith("Z"):
            raise ValueError("evaluated_at must be ISO-8601 UTC with a 'Z' suffix")
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return value

    @property
    def staleness_seconds(self) -> float:
        evaluated = datetime.fromisoformat(self.evaluated_at.replace("Z", "+00:00"))
        advertised = datetime.fromisoformat(
            self.advertisement.advertised_at.replace("Z", "+00:00")
        )
        return (evaluated - advertised).total_seconds()


class ModelMemoryAdmissionReceipt(BaseModel):
    """Typed admission outcome — refusal is ALWAYS a receipt, never a silent queue."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: EnumMemoryAdmissionOutcome = Field(..., description="Admitted or refused.")
    host_identity: str = Field(..., min_length=1, description="Evaluated host echo.")
    requested_job_memory_bytes: int = Field(
        ..., gt=0, description="Echo of the requested job memory."
    )
    usable_bytes: int = Field(
        ...,
        ge=0,
        description="usable = total - wired - inference_reservation "
        "at evaluation time, floored at 0.",
    )
    headroom_bytes: int | None = Field(
        default=None,
        description="Remaining usable bytes AFTER admitting the job "
        "(outcome=admitted); None when refused.",
    )
    staleness_seconds: float = Field(
        ..., description="evaluated_at - advertised_at, seconds."
    )
    staleness_bound_seconds: int = Field(
        ..., description="2 * advertisement.cadence_seconds — the fail-closed bound."
    )
    refusal_reason: EnumMemoryAdmissionRefusalReason | None = Field(
        default=None, description="Typed reason; None iff outcome=admitted."
    )
    evaluated_at: str = Field(..., description="Echo of the evaluation timestamp.")

    @model_validator(mode="after")
    def _enforce_outcome_invariants(self) -> ModelMemoryAdmissionReceipt:
        if self.outcome is EnumMemoryAdmissionOutcome.ADMITTED:
            if self.refusal_reason is not None:
                raise ValueError("outcome=admitted requires refusal_reason=None")
            if self.headroom_bytes is None:
                raise ValueError(
                    "outcome=admitted requires a non-None headroom_bytes "
                    "(remaining usable bytes after admission)"
                )
        else:  # REFUSED
            if self.refusal_reason is None:
                raise ValueError(
                    "outcome=refused requires a typed refusal_reason — never "
                    "a silent/unexplained refusal"
                )
            if self.headroom_bytes is not None:
                raise ValueError(
                    "outcome=refused requires headroom_bytes=None (no "
                    "admission headroom was allocated)"
                )
        return self


def should_collapse(
    current_usable_bytes: int,
    floor_bytes: int,
    *,
    collapse_policy: EnumMidRunCollapsePolicy,
) -> bool:
    """Mid-suite collapse decision primitive (plan §2 D3).

    Pure function so the mid-run headroom watchdog (an EFFECT concern — it
    polls live headroom while a suite runs, out of scope for this ticket) has
    a single, tested decision point instead of re-deriving the policy inline.
    Returns True iff the caller should abort the in-flight suite now
    (``collapse_policy=abort`` and headroom has crossed the floor);
    ``collapse_policy=finish`` never collapses mid-run — it lets the suite run
    to completion once started, by construction.
    """
    if collapse_policy is EnumMidRunCollapsePolicy.FINISH:
        return False
    return current_usable_bytes < floor_bytes


__all__ = [
    "EnumMemoryAdmissionOutcome",
    "EnumMemoryAdmissionRefusalReason",
    "EnumMidRunCollapsePolicy",
    "ModelHostMemoryAdvertisement",
    "ModelMemoryAdmissionReceipt",
    "ModelMemoryAdmissionRequest",
    "should_collapse",
]
