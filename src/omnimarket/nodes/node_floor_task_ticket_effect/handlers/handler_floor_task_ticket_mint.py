# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Find or create the one ticket of a floor task: search by the Floor key line, then create, every time.

Fail closed, retry on the next tick. Each call of the store runs under ``floor_fill.mint_timeout_seconds``:

(a) ``search_by_floor_key``: the oldest match that carries the exact Floor key line, is not Canceled or Done and is not an
    umbrella is used and nothing is created, unless the old fill can see it (a project, a label naming delegation or a
    pillar): then it is neither used nor replaced and the mint ends ``ticket-refused:old-fill-visible``.
(b) no match: ``create`` once, then one ``read`` of the created issue (no retry loop) that must show it usable.
(c) a call that fails or runs past the deadline, and a read-back that is unreadable or unusable, ends the mint for the tick
    with no ticket (``unavailable``); the next tick searches first again.

The gate runs inside the store's ``create`` (once); a payload it refuses comes back as ``ticket-refused:<codes>``.
"""

from __future__ import annotations

from collections.abc import Callable

from omnibase_spi.contracts.services import (
    ModelFloorTicketCreateRequest,
    ModelFloorTicketLookupRequest,
    ModelFloorTicketLookupResult,
    ModelFloorTicketSearchRequest,
)
from omnibase_spi.protocols.lane import ProtocolFloorTicketStore

from omnimarket.nodes.node_floor_task_ticket_effect.handlers.bounded_call import (
    CallDeadlineError,
    CallFailedError,
    bounded,
)
from omnimarket.nodes.node_floor_task_ticket_effect.handlers.ticket_judge import (
    judge,
    oldest_first,
)
from omnimarket.nodes.node_floor_task_ticket_effect.models import (
    ModelFloorTaskTicketMintRequest,
    ModelFloorTaskTicketMintResult,
)

REFUSED_PREFIX = "ticket-refused:"
OLD_FILL_VISIBLE = "old-fill-visible"


class _EndOfMintError(Exception):
    """The mint ends for this tick with this result."""

    def __init__(self, result: ModelFloorTaskTicketMintResult) -> None:
        super().__init__(result.cause)
        self.result = result


def _unavailable(cause: str) -> _EndOfMintError:
    return _EndOfMintError(
        ModelFloorTaskTicketMintResult(state="unavailable", cause=cause)
    )


class HandlerFloorTaskTicketMint:
    """Search by the Floor key line, then create; the ticket store is injected."""

    def __init__(self, store: ProtocolFloorTicketStore) -> None:
        self._store = store

    def handle(
        self, request: ModelFloorTaskTicketMintRequest
    ) -> ModelFloorTaskTicketMintResult:
        try:
            used = self._search(request)
            if used is not None:
                return ModelFloorTaskTicketMintResult(
                    state="ticket", ticket=used, reused=True
                )
            return self._create(request)
        except _EndOfMintError as ended:
            return ended.result

    def _call[Request, Result](
        self,
        operation: str,
        call: Callable[[Request], Result],
        argument: Request,
        request: ModelFloorTaskTicketMintRequest,
    ) -> Result:
        try:
            return bounded(call, argument, request.mint_timeout_seconds)
        except CallDeadlineError:
            raise _unavailable(f"{operation}-timeout") from None
        except CallFailedError as failed:
            raise _unavailable(f"{operation}-failed:{failed.cause}") from None

    def _read(
        self, ticket: str, operation: str, request: ModelFloorTaskTicketMintRequest
    ) -> ModelFloorTicketLookupResult:
        return self._call(
            operation,
            self._store.read,
            ModelFloorTicketLookupRequest(ticket=ticket),
            request,
        )

    def _search(self, request: ModelFloorTaskTicketMintRequest) -> str | None:
        searched = self._call(
            "search",
            self._store.search_by_floor_key,
            ModelFloorTicketSearchRequest(floor_key=request.floor_key),
            request,
        )
        if searched.state == "failed":
            raise _unavailable(f"search-failed:{searched.cause}")
        for ticket in oldest_first(searched.tickets):
            found = self._read(ticket, "read", request)
            if found.state == "failed":
                raise _unavailable(f"read-failed:{found.cause}")
            if found.state == "missing":
                raise _unavailable("read-missing")
            verdict = judge(found, request.floor_key)
            if verdict == "usable":
                return ticket
            if verdict == OLD_FILL_VISIBLE:
                raise _EndOfMintError(
                    ModelFloorTaskTicketMintResult(
                        state="refused", cause=f"{REFUSED_PREFIX}{OLD_FILL_VISIBLE}"
                    )
                )
        return None

    def _create(
        self, request: ModelFloorTaskTicketMintRequest
    ) -> ModelFloorTaskTicketMintResult:
        created = self._call(
            "create",
            self._store.create,
            ModelFloorTicketCreateRequest(
                floor_key=request.floor_key,
                title=request.title,
                description=request.description,
                parent=request.parent,
            ),
            request,
        )
        if created.state == "refused":
            return ModelFloorTaskTicketMintResult(
                state="refused", cause=f"{REFUSED_PREFIX}{created.cause}"
            )
        if created.state == "failed" or created.ticket is None:
            raise _unavailable(f"create-failed:{created.cause}")
        read_back = self._read(created.ticket, "readback", request)
        if read_back.state != "found":
            raise _unavailable("readback-unreadable")
        verdict = judge(read_back, request.floor_key)
        if verdict != "usable":
            raise _unavailable(f"readback-unusable:{verdict}")
        return ModelFloorTaskTicketMintResult(state="ticket", ticket=created.ticket)
