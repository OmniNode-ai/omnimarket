# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A recording ticket store that can hold a call past its deadline, complete a create late and lag a search."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import ClassVar

from omnibase_spi.contracts.services import (
    ModelFloorTicketCreateRequest,
    ModelFloorTicketCreateResult,
    ModelFloorTicketLookupRequest,
    ModelFloorTicketLookupResult,
    ModelFloorTicketSearchRequest,
    ModelFloorTicketSearchResult,
)

from omnimarket.nodes.node_floor_task_ticket_effect.adapters.adapter_inmemory_floor_ticket_store import (
    InMemoryFloorTicketStore,
    StoredTicket,
)
from omnimarket.nodes.node_floor_task_ticket_effect.models import (
    ModelFloorTaskTicketMintRequest,
)

KEY = "floor:line-a:T1"
PARENT = "OMN-70001"
TITLE = "T1. The example node"
DESCRIPTION = f"Gate: {PARENT} AC-1\n\nFloor key: {KEY}\n\n## Acceptance criteria\n\n- [ ] AC1: x -- falsifier: y\n"
DEADLINE = 0.2


def mint_request(
    key: str = KEY,
    description: str | None = None,
    timeout: float = DEADLINE,
) -> ModelFloorTaskTicketMintRequest:
    return ModelFloorTaskTicketMintRequest(
        floor_key=key,
        title=TITLE,
        description=description
        if description is not None
        else DESCRIPTION.replace(KEY, key),
        parent=PARENT,
        mint_timeout_seconds=timeout,
    )


def ticket_text(key: str = KEY) -> str:
    return f"Floor key: {key}\n"


class ScriptedStore:
    """The in-memory store behind a call log, with the failures the world has: a held call, a late create, a lag."""

    synchronous_execution: ClassVar[bool] = True

    def __init__(self, inner: InMemoryFloorTicketStore | None = None) -> None:
        self.inner = (
            inner if inner is not None else InMemoryFloorTicketStore(first_number=70100)
        )
        self.calls: list[str] = []
        self.holds: dict[str, threading.Event] = {}
        self.lag_search = False
        self.raise_on: dict[str, Exception] = {}
        self.read_after_create: ModelFloorTicketLookupResult | None = None
        self.search_failure: str | None = None
        self.created_through_create: list[str] = []

    def add(self, ticket: str, **fields: object) -> None:
        self.inner.tickets[ticket] = StoredTicket(**fields)  # type: ignore[arg-type]

    def hold(self, operation: str) -> threading.Event:
        """Make every call of ``operation`` wait on the returned event before it runs."""
        event = threading.Event()
        self.holds[operation] = event
        return event

    def release_all(self) -> None:
        for event in self.holds.values():
            event.set()

    def _enter(self, operation: str) -> None:
        self.calls.append(operation)
        failure = self.raise_on.get(operation)
        if failure is not None:
            raise failure
        event = self.holds.get(operation)
        if event is not None:
            event.wait(timeout=30)

    @property
    def creates(self) -> int:
        return self.calls.count("create")

    def read(
        self, request: ModelFloorTicketLookupRequest
    ) -> ModelFloorTicketLookupResult:
        self._enter("read")
        if (
            self.read_after_create is not None
            and request.ticket in self.created_through_create
        ):
            return self.read_after_create
        return self.inner.read(request)

    def search_by_floor_key(
        self, request: ModelFloorTicketSearchRequest
    ) -> ModelFloorTicketSearchResult:
        self._enter("search_by_floor_key")
        if self.search_failure is not None:
            return ModelFloorTicketSearchResult(
                state="failed", cause=self.search_failure
            )
        if self.lag_search:
            return ModelFloorTicketSearchResult(state="answered")
        return self.inner.search_by_floor_key(request)

    def create(
        self, request: ModelFloorTicketCreateRequest
    ) -> ModelFloorTicketCreateResult:
        self._enter("create")
        result = self.inner.create(request)
        if result.ticket is not None:
            self.created_through_create.append(result.ticket)
        return result


@contextmanager
def released(store: ScriptedStore) -> Iterator[ScriptedStore]:
    """Hand the store over and release every held call at the end, so no abandoned call outlives the test."""
    try:
        yield store
    finally:
        store.release_all()
