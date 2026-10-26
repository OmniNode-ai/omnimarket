# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Live Linear facts and send verdicts for idle lab slots."""

from __future__ import annotations

from collections import Counter
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class EnumLabFillLiveSkip(StrEnum):
    """The first live gate that excludes a ticket from a send."""

    CLOSED = "closed"
    OTHER_OWNER = "other-owner"
    NOT_FOUND = "not-found"
    UNREADABLE = "live-unreadable"


class ModelLabFillLiveTicket(BaseModel):
    """One ticket as Linear reads it just before a send."""

    model_config = _FROZEN

    id: str = Field(pattern=r"^OMN-[0-9]+$")
    state_type: str = ""
    state_name: str = ""
    assignee_id: str | None = None
    assignee_name: str | None = None


class ModelLabFillLiveFacts(BaseModel):
    """Requested tickets, known operator identities and the live read outcome."""

    model_config = _FROZEN

    tickets: tuple[Annotated[str, Field(pattern=r"^OMN-[0-9]+$")], ...]
    operators: tuple[str, ...] = ()
    live: tuple[ModelLabFillLiveTicket, ...] = ()
    error: str = ""


class ModelLabFillLiveVerdict(BaseModel):
    """One ticket's send decision with its first exclusion reason."""

    model_config = _FROZEN

    id: str
    send: bool
    reason: EnumLabFillLiveSkip | None = None
    detail: str = ""


class ModelLabFillLivePlan(BaseModel):
    """Request-ordered send decisions and stable skip diagnostics."""

    model_config = _FROZEN

    verdicts: tuple[ModelLabFillLiveVerdict, ...] = ()

    def send_ids(self) -> tuple[str, ...]:
        """Return admitted ticket ids in verdict order."""
        return tuple(verdict.id for verdict in self.verdicts if verdict.send)

    def skip_counts(self) -> dict[str, int]:
        """Count skipped verdicts by reason value, sorted by key."""
        return dict(
            sorted(
                Counter(
                    verdict.reason.value
                    for verdict in self.verdicts
                    if not verdict.send and verdict.reason is not None
                ).items()
            )
        )


class ModelLabFillLiveGateRequest(BaseModel):
    """Tickets to check immediately before dispatch and an optional operator."""

    model_config = _FROZEN

    tickets: tuple[Annotated[str, Field(pattern=r"^OMN-[0-9]+$")], ...]
    operator: str | None = None
