# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Commands, entry states and decision reasons of the review quota reducer (OMN-21187)."""

from __future__ import annotations

from enum import StrEnum, unique


@unique
class EnumReviewQuotaCommand(StrEnum):
    """What the caller asks the reducer to do with one request id."""

    RESERVE = "reserve"
    """Ask for room in the route's quota window before a covered model call."""

    CONSUME = "consume"
    """Mark a reservation as spent: the model call ran."""

    RELEASE = "release"
    """Return a reservation that never reached the model."""


@unique
class EnumReviewQuotaEntryStatus(StrEnum):
    """A recorded request; both states count against the quota."""

    RESERVED = "reserved"
    CONSUMED = "consumed"


@unique
class EnumReviewQuotaReason(StrEnum):
    """Why the reducer applied or refused a command; the first five apply it."""

    ADMITTED = "admitted"
    """RESERVE: the route's quota window had room."""

    REPLAYED = "replayed"
    """The command was already applied for this request id; nothing changed."""

    CONSUMED = "consumed"
    RELEASED = "released"

    QUOTA_EXHAUSTED = "quota_exhausted"
    """RESERVE: the route's quota window is full."""

    ROUTE_NOT_ELIGIBLE = "route_not_eligible"
    """The route is not on the preregistered list."""

    COLLECTION_NOT_STARTED = "collection_not_started"
    """No collection-start instant was supplied, or the clock is before it."""

    COLLECTION_CLOSED = "collection_closed"
    """The clock is past the preregistered collection window."""

    PREREGISTRATION_MISMATCH = "preregistration_mismatch"
    """The request names a preregistration hash other than the sealed one."""

    REQUEST_ID_CONFLICT = "request_id_conflict"
    """The request id is recorded against another route."""

    UNKNOWN_REQUEST = "unknown_request"
    """CONSUME or RELEASE named a request id the state does not hold."""

    ALREADY_CONSUMED = "already_consumed"
    """RELEASE named a reservation that was already consumed."""
