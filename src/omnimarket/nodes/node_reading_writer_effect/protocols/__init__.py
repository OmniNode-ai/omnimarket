# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ports of node_reading_writer_effect: where ledger rows come from and where readings go."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.model_reading import (
    ModelLandingRequirements,
    ModelLandingSnapshot,
    ModelLedgerEvidenceRow,
)


class ModelLedgerQuery(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    since: datetime
    until: datetime
    row_type: str
    lane: str | None = None
    term: str | None = None


class ModelLedgerRead(BaseModel):
    """Rows, or the reason they could not be read. An error is never an empty answer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rows: tuple[ModelLedgerEvidenceRow, ...] = ()
    error: str | None = None


class ModelAppendOutcome(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["accepted", "duplicate", "refused", "pending", "error"]
    message: str = Field(default="", max_length=2000)

    @property
    def ok(self) -> bool:
        return self.status in ("accepted", "duplicate")


class ModelLandingRead(BaseModel):
    """The landing snapshot, or the reason it could not be read. An error is never an empty queue."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    snapshot: ModelLandingSnapshot | None = None
    error: str | None = None


class ProtocolLandingSource(Protocol):
    """Read the pull request watcher against the landing goal's scope, as of ``now``."""

    def read(
        self, requirements: ModelLandingRequirements, now: datetime
    ) -> ModelLandingRead: ...


class ProtocolLedgerRowSource(Protocol):
    """Read ledger rows through the route the deployment configures."""

    def read(self, query: ModelLedgerQuery) -> ModelLedgerRead: ...


class ProtocolReadingAppender(Protocol):
    """Append reading rows. Redelivery under one request id must not write twice."""

    def append(self, rows: str, request_id: str) -> ModelAppendOutcome: ...


__all__ = [
    "ModelAppendOutcome",
    "ModelLandingRead",
    "ModelLedgerQuery",
    "ModelLedgerRead",
    "ProtocolLandingSource",
    "ProtocolLedgerRowSource",
    "ProtocolReadingAppender",
]
