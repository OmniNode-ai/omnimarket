# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""The record written beside a quarantined spool file (OMN-18627).

A quarantine that leaves no reason is indistinguishable from a lost record, so
the reason is a contract rather than a log line: it is what a later reconcile
reads to decide whether the record can now be replayed, and it names the topic
whose grant is missing so a reader does not have to infer it from the payload.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ModelQuarantineReason(BaseModel):
    """Why one spool record was moved out of the pending queue."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = Field(
        default=1,
        ge=1,
        description="Bumped when this record's shape changes, so a reconcile "
        "reading an older quarantine refuses it rather than guessing.",
    )
    quarantined_at: datetime = Field(
        ...,
        description="UTC, tz-aware. When the drain moved the record aside.",
    )
    reason_code: str = Field(
        ...,
        min_length=1,
        description="Machine-readable class of refusal, e.g. "
        "'topic_authorization_denied'.",
    )
    detail: str = Field(
        ...,
        min_length=1,
        description="The broker's own message, sanitized by the transport "
        "before it reaches here.",
    )
    event_id: str = Field(..., min_length=1)
    event_type: str = Field(..., min_length=1)
    topic: str = Field(
        ...,
        min_length=1,
        description="The topic the publish was refused for -- the grant a "
        "reader has to provision, named rather than inferred.",
    )
    queued_at: datetime = Field(
        ...,
        description="When the record was originally spooled, so the age of a "
        "quarantined record is readable without parsing its filename.",
    )


__all__: list[str] = ["ModelQuarantineReason"]
