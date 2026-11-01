# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Read a ticket's body for the criteria-drift check through the project tracker (OMN-21676).

Before this module the verifier opened its own Linear connection with a credential it read
from the environment by name. It now takes a ``ProtocolProjectTracker``-shaped tracker, the
one the ticketing nodes read tickets through, so the credential is the secret store's and the
connection is the tracker's.

A failure is a typed :class:`ModelTicketCriteriaRead` carrying a reason, never an empty body:
an unreadable ticket must refuse, where the OCC transcriber's reader may swallow an outage.
A reason names the failure class only, because an exception raised by an HTTP client can carry
the request it failed on, headers included.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Protocol

from omnimarket.nodes.node_dod_verify.models.model_ticket_criteria_read import (
    ModelTicketCriteriaRead,
)

__all__ = [
    "ProtocolCriteriaIssue",
    "ProtocolCriteriaTracker",
    "TrackerCriteriaReader",
    "read_ticket_through_tracker",
]


class ProtocolCriteriaIssue(Protocol):
    """The two fields of a tracker issue the criteria check reads."""

    @property
    def identifier(self) -> str: ...

    @property
    def description(self) -> str | None: ...


class ProtocolCriteriaTracker(Protocol):
    """The slice of ``ProtocolProjectTracker`` a ticket-body read uses."""

    async def get_issue(self, issue_id: str) -> ProtocolCriteriaIssue: ...

    async def close(self) -> None: ...


async def read_ticket_through_tracker(
    tracker: ProtocolCriteriaTracker, ticket_id: str
) -> ModelTicketCriteriaRead:
    """The ticket body from ``tracker``, or the reason it could not be read."""
    try:
        issue = await tracker.get_issue(ticket_id)
    except KeyError:
        return _unavailable(ticket_id, "the tracker holds no such ticket")
    except Exception as error:
        return _unavailable(
            ticket_id, f"the tracker read failed ({type(error).__name__})"
        )
    if issue.identifier != ticket_id:
        return _unavailable(ticket_id, "the tracker returned a different ticket")
    description = issue.description
    return ModelTicketCriteriaRead(
        ticket_id=ticket_id,
        description=description if isinstance(description, str) else "",
    )


def _unavailable(ticket_id: str, reason: str) -> ModelTicketCriteriaRead:
    return ModelTicketCriteriaRead(ticket_id=ticket_id, unavailable_reason=reason)


def _run_blocking(work: Awaitable[ModelTicketCriteriaRead]) -> ModelTicketCriteriaRead:
    """Run ``work`` to completion from sync code, on a worker thread when a loop is running."""

    async def _await() -> ModelTicketCriteriaRead:
        return await work

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_await())
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, _await()).result()


class TrackerCriteriaReader:
    """A ``ProtocolDodTicketCriteriaReader`` over a tracker built per read.

    The tracker comes from an async factory (the secret store resolves its credential), is used
    for one read and is closed, so no connection outlives the loop it was opened on. A factory
    that raises is a ticket that could not be read.
    """

    def __init__(
        self, tracker_factory: Callable[[], Awaitable[ProtocolCriteriaTracker]]
    ):
        self._tracker_factory = tracker_factory

    def read(self, ticket_id: str) -> ModelTicketCriteriaRead:
        """The ticket body, or the reason it could not be read."""
        return _run_blocking(self._read(ticket_id))

    async def _read(self, ticket_id: str) -> ModelTicketCriteriaRead:
        try:
            tracker = await self._tracker_factory()
        except Exception as error:
            return _unavailable(
                ticket_id, f"no ticket tracker could be built ({type(error).__name__})"
            )
        try:
            return await read_ticket_through_tracker(tracker, ticket_id)
        finally:
            with contextlib.suppress(Exception):  # a failed close changes no verdict
                await tracker.close()
