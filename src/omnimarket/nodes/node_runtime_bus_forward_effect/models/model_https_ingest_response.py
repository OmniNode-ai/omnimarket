# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""The door's indexed acknowledgments and refusals as read by the sender."""

from __future__ import annotations

from pydantic import AliasChoices, BaseModel, ConfigDict, Field


class ModelIngestAcknowledgment(BaseModel):
    """One record position and its identity in the request that was answered."""

    model_config = ConfigDict(strict=True, frozen=True)
    index: int = Field(ge=0)
    event_id: str


class ModelIngestFailure(ModelIngestAcknowledgment):
    """A record that reached the door's publish phase and failed."""

    reason: str
    retryable: bool


class ModelIngestRefusal(BaseModel):
    """A validation reason for a record in a refused batch."""

    model_config = ConfigDict(strict=True, frozen=True)
    index: int = Field(ge=0)
    reason: str


class ModelHttpsIngestResponse(BaseModel):
    """The decision fields present on success, partial outcomes, or refusals."""

    model_config = ConfigDict(strict=True, frozen=True, populate_by_name=True)
    published: tuple[ModelIngestAcknowledgment, ...] | int | None = None
    failed: tuple[ModelIngestFailure, ...] = ()
    refusals: tuple[ModelIngestRefusal, ...] = Field(
        default=(),
        validation_alias=AliasChoices("refusals", "errors", "refused", "rejected"),
    )
    reason: str | None = None
    retryable: bool | None = None
