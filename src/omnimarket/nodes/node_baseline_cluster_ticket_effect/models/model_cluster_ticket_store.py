# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and result models of the cluster ticket store: one frozen pair per synchronous call."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class ModelClusterTicketSearchRequest(BaseModel):
    """Search the tracker for tickets whose description carries any of the needles.

    The needles are the cluster's signature line and its legacy ``**Cluster:**`` line; the
    tracker matches whole descriptions containing them, never the ticket title.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    needles: tuple[str, ...]


class ModelClusterTicketSearchResult(BaseModel):
    """What the search answered: the tickets found, or the failure that ends the report."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: Literal["answered", "failed"]
    tickets: tuple[str, ...] = ()
    cause: str | None = None


class ModelClusterTicketLookupRequest(BaseModel):
    """Read one ticket: its status, description and, for a duplicate, the surviving ticket."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket: str


class ModelClusterTicketLookupResult(BaseModel):
    """What the read answered: the ticket's state, or ``missing``, or the failure of the read.

    ``status``: ``open`` is any non-terminal state; ``done`` is completed; ``duplicate`` is
    closed as a duplicate (``duplicate_of`` names the surviving ticket when the tracker says
    so); ``canceled`` is any other cancellation. ``description`` is empty unless found.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: Literal["found", "missing", "failed"]
    ticket: str | None = None
    status: Literal["open", "done", "canceled", "duplicate"] | None = None
    description: str = ""
    duplicate_of: str | None = None
    cause: str | None = None


class ModelClusterTicketCreateRequest(BaseModel):
    """Create one ticket under a parent, with the title and description the gate admits."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str
    description: str
    parent: str


class ModelClusterTicketCreateResult(BaseModel):
    """What the create answered: the new ticket, a refusal a person fixes, or a failure."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: Literal["created", "refused", "failed"]
    ticket: str | None = None
    cause: str | None = None


class ModelClusterTicketCommentRequest(BaseModel):
    """Append one comment to a ticket: the new occurrence of the cluster."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket: str
    body: str


class ModelClusterTicketCommentResult(BaseModel):
    """What the comment answered: the comment's id, or the failure of the comment."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: Literal["commented", "failed"]
    comment_id: str | None = None
    cause: str | None = None
