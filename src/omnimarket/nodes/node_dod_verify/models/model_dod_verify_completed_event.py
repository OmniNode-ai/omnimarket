"""ModelDodVerifyCompletedEvent — emitted when DoD verification finishes."""

from __future__ import annotations

from datetime import datetime
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.enums.enum_dod_verify_unresolved_cause import (
    EnumDodVerifyUnresolvedCause,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumDodVerifyStatus,
    ModelEvidenceCheckResult,
)


class ModelDodVerifyCompletedEvent(BaseModel):
    """Final event when DoD verification finishes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(...)
    ticket_id: str = Field(...)
    status: EnumDodVerifyStatus = Field(...)
    started_at: datetime = Field(...)
    completed_at: datetime = Field(...)
    checks: list[ModelEvidenceCheckResult] = Field(default_factory=list)
    total_checks: int = Field(default=0, ge=0)
    verified_count: int = Field(default=0, ge=0)
    failed_count: int = Field(default=0, ge=0)
    skipped_count: int = Field(default=0, ge=0)
    superseded_count: int = Field(default=0, ge=0)
    # OMN-15391: executed, exited 0, but exit-status-invariant over the product
    # change. Provenance, never completion — see ModelDodVerifyState.
    non_probative_count: int = Field(default=0, ge=0)
    # OMN-15911: verdict-bearing checks that PASSED *and* executed the claimed
    # behavior. Carried on the terminal event so a bus consumer sees the same
    # discrimination the state does, without re-deriving it from `checks`.
    behavior_proving_count: int = Field(default=0, ge=0)
    # OMN-17323: verifier-derived ``::pr-live-state`` overlays with no binding,
    # excluded from ``total_checks``. Carried on the terminal event for the same
    # reason ``superseded_count`` is — so a bus consumer can see the exclusion
    # in the denominator instead of inferring it from a counter mismatch.
    unbindable_overlay_count: int = Field(default=0, ge=0)
    error_message: str | None = Field(default=None)
    # OMN-17022: carried on the terminal event so a bus consumer branches on
    # the typed cause instead of re-parsing ``error_message``. Set exactly
    # when ``status`` is UNRESOLVED, mirroring ModelDodVerifyState.
    unresolved_cause: EnumDodVerifyUnresolvedCause | None = Field(default=None)

    @model_validator(mode="after")
    def _cause_pairs_with_unresolved(self) -> Self:
        unresolved = self.status is EnumDodVerifyStatus.UNRESOLVED
        if unresolved != (self.unresolved_cause is not None):
            raise ValueError(
                "unresolved_cause is set exactly when status is UNRESOLVED; "
                f"got status={self.status.value}, "
                f"unresolved_cause={self.unresolved_cause!r} for {self.ticket_id}"
            )
        return self


__all__: list[str] = ["ModelDodVerifyCompletedEvent"]
