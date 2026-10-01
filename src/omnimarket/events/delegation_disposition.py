# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Content-free, producer-timed delegation disposition event (OMN-20242)."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Self
from uuid import UUID, uuid5

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from omnimarket.enums.enum_delegation_disposition import (
    DISPOSITION_REASONS,
    EnumDelegationArtifactKind,
    EnumDelegationDisposition,
    EnumDelegationDispositionReason,
)
from omnimarket.events.topics import DELEGATION_DISPOSITION_RECORDED_TOPIC_V1
from omnimarket.models.delegation.delegation_caller_lane import CALLER_LANE_PATTERN
from omnimarket.models.delegation.delegation_ticket_id import TICKET_ID_PATTERN

TOPIC = DELEGATION_DISPOSITION_RECORDED_TOPIC_V1
DISPOSITION_ID_NAMESPACE = UUID("d12c8717-b935-51e6-b812-c275c2407d13")


def disposition_id_for(
    delegation_correlation_id: UUID, caller_lane: str, recorded_at: datetime
) -> UUID:
    """Stable delivery key, independent of the timestamp's timezone spelling."""
    if recorded_at.tzinfo is None or recorded_at.utcoffset() is None:
        raise ValueError("recorded_at must be timezone-aware")
    return uuid5(
        DISPOSITION_ID_NAMESPACE,
        f"{delegation_correlation_id}|{caller_lane}|{recorded_at.astimezone(UTC).isoformat()}",
    )


class ModelDelegationDispositionRecorded(BaseModel):
    """The judged answer is identified only by a digest, never by its content."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: UUID
    delegation_correlation_id: UUID
    disposition: EnumDelegationDisposition
    reason_code: EnumDelegationDispositionReason
    caller_lane: str = Field(pattern=CALLER_LANE_PATTERN.pattern)
    engine: str | None = Field(
        default=None, max_length=64, pattern=r"^[a-z0-9][a-z0-9._:-]*$"
    )
    artifact_kind: EnumDelegationArtifactKind
    artifact_ref: str | None = Field(default=None, max_length=512)
    edit_ratio: float | None = Field(default=None, ge=0, le=1)
    ticket_id: str | None = Field(default=None, pattern=TICKET_ID_PATTERN.pattern)
    answer_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    recorded_at: AwareDatetime
    disposition_id: UUID

    @field_validator("caller_lane")
    @classmethod
    def _validate_lane(cls, value: str) -> str:
        if CALLER_LANE_PATTERN.fullmatch(value) is None:
            raise ValueError("caller_lane must be a ledger lane token")
        return value

    @field_validator("ticket_id")
    @classmethod
    def _validate_ticket(cls, value: str | None) -> str | None:
        if value is not None and TICKET_ID_PATTERN.fullmatch(value) is None:
            raise ValueError("ticket_id must be a ticket identifier")
        return value

    @model_validator(mode="after")
    def _validate_disposition(self) -> Self:
        if self.reason_code not in DISPOSITION_REASONS[self.disposition]:
            raise ValueError("reason_code does not belong to disposition")
        if (self.disposition == EnumDelegationDisposition.EDITED) != (
            self.edit_ratio is not None
        ):
            raise ValueError(
                "edit_ratio is required exactly when disposition is edited"
            )
        if (self.artifact_kind == EnumDelegationArtifactKind.NONE) != (
            self.artifact_ref is None
        ):
            raise ValueError(
                "artifact_kind NONE requires no artifact_ref; other kinds require a ref"
            )
        if (
            self.disposition
            in (
                EnumDelegationDisposition.ACCEPTED_AS_IS,
                EnumDelegationDisposition.EDITED,
            )
            and self.artifact_kind == EnumDelegationArtifactKind.NONE
        ):
            raise ValueError("accepted or edited output must name an artifact")
        ref = self.artifact_ref
        if ref is not None:
            if self.artifact_kind == EnumDelegationArtifactKind.PULL_REQUEST:
                if (
                    re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[1-9][0-9]*", ref)
                    is None
                ):
                    raise ValueError("pull_request ref must be owner/repo#number")
            elif self.artifact_kind == EnumDelegationArtifactKind.COMMIT:
                if re.fullmatch(r"[0-9a-f]{7,40}", ref) is None:
                    raise ValueError(
                        "commit ref must be 7..40 lowercase hex characters"
                    )
            elif self.artifact_kind == EnumDelegationArtifactKind.DOCUMENT and (
                not ref
                or ref != ref.strip()
                or len(ref.splitlines()) != 1
                or ".." in re.split(r"[/\\]", ref)
            ):
                raise ValueError(
                    "document ref must be one trimmed line with no parent segment"
                )
        if self.disposition_id != disposition_id_for(
            self.delegation_correlation_id, self.caller_lane, self.recorded_at
        ):
            raise ValueError(
                "disposition_id does not match correlation, lane and recorded_at"
            )
        return self

    @classmethod
    def build(
        cls,
        *,
        tenant_id: UUID,
        delegation_correlation_id: UUID,
        disposition: EnumDelegationDisposition,
        reason_code: EnumDelegationDispositionReason,
        caller_lane: str,
        artifact_kind: EnumDelegationArtifactKind,
        recorded_at: datetime,
        engine: str | None = None,
        artifact_ref: str | None = None,
        edit_ratio: float | None = None,
        ticket_id: str | None = None,
        answer_sha256: str | None = None,
    ) -> Self:
        """Build a validated event with its deterministic delivery key."""
        return cls(
            tenant_id=tenant_id,
            delegation_correlation_id=delegation_correlation_id,
            disposition=disposition,
            reason_code=reason_code,
            caller_lane=caller_lane,
            engine=engine,
            artifact_kind=artifact_kind,
            artifact_ref=artifact_ref,
            edit_ratio=edit_ratio,
            ticket_id=ticket_id,
            answer_sha256=answer_sha256,
            recorded_at=recorded_at,
            disposition_id=disposition_id_for(
                delegation_correlation_id, caller_lane, recorded_at
            ),
        )


__all__ = [
    "DISPOSITION_ID_NAMESPACE",
    "TOPIC",
    "ModelDelegationDispositionRecorded",
    "disposition_id_for",
]
