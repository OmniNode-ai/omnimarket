# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One record of the agent message topic as the reader hands it over, and the ingest request and result."""

from __future__ import annotations

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class ModelAgentMessageDelivery(BaseModel):
    """A topic record with its source coordinates.

    ``payload`` is the decoded record when the runtime gave one; ``raw`` is the record's bytes when the reader
    holds them. Neither being present is a dropped message, not an error.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    topic: str = Field(min_length=1)
    partition: int = Field(ge=0)
    offset: int = Field(ge=0)
    broker_time: AwareDatetime | None = Field(
        default=None,
        description="The broker's timestamp of the record, when the reader has it.",
    )
    payload: object | None = None
    raw: bytes | None = None


class ModelAgentMessageIngestRequest(BaseModel):
    """Read at most ``max_count`` records from the injected reader and project them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_count: int = Field(default=500, ge=1, le=10000)


class ModelAgentMessageIngestResult(BaseModel):
    """What one ingest pass did; every record read is in exactly one of the three counts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    read: int = Field(ge=0)
    projected: int = Field(ge=0, description="New rows for readable messages.")
    refused: int = Field(
        ge=0, description="New rows for dropped, unreadable or refused messages."
    )
    duplicates: int = Field(
        ge=0, description="Records whose partition and offset already have a row."
    )
