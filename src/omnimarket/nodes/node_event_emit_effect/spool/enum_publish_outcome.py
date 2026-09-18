# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""How one publish attempt ended, for the drain to act on (OMN-18627).

The drain used to decide from a bool, and a bool cannot express the difference
that matters: whether retrying this exact record could EVER succeed.

Measured cost of not expressing it. The spool drain stops at the first failure
to preserve ordering, which is correct for a transient failure -- publishing the
next record while this one is still owed would reorder the stream. It is wrong
for a record the broker will refuse every time. One record naming a topic its
principal had no WRITE grant on sat at the head of the spool for 20 hours,
re-tried every cycle, holding 126 records of four AUTHORIZED classes behind it.
"""

from __future__ import annotations

from enum import StrEnum


class EnumPublishOutcome(StrEnum):
    """Terminal state of one ``_try_publish`` attempt."""

    PUBLISHED = "published"
    """The broker accepted it and the spool file has been acked."""

    RETRYABLE = "retryable"
    """A timeout, a connection failure, an exhausted budget. The record stays
    on disk and the drain STOPS, because the next record cannot be published
    ahead of one that is still owed without reordering the stream."""

    UNPUBLISHABLE = "unpublishable"
    """The broker answered with a verdict that cannot change on a retry --
    today, only an authorization refusal on the topic's ACLs. The record is
    quarantined (moved, never deleted) and the drain CONTINUES, because a
    record that will never go out is not an ordering constraint on the records
    behind it; it is a record that has left the stream."""


__all__: list[str] = ["EnumPublishOutcome"]
