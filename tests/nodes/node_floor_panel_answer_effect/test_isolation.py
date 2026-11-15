# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The new panel never changes or delays the old floor route (OMN-19998, finding M3 of the revision 9 review).

A new-panel handler that merely raises beside an uncoupled old route leaves the old route unchanged, so that alone is not a control.
The checks run the old route under a deadline while the new panel handler fails or blocks, and the controls couple the two routes.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import pytest

from omnimarket.handlers.floor_document_store import InMemoryFloorDocumentStore
from omnimarket.nodes.node_floor_page_api_effect.handlers.handler_floor_page import (
    HandlerFloorPage,
)
from omnimarket.nodes.node_floor_page_api_effect.models.model_floor_page_request import (
    ModelFloorPageRequest,
)
from omnimarket.nodes.node_floor_panel_answer_effect.protocols.adapter_floor_panel_row_reader_memory import (
    InMemoryFloorPanelRowReader,
    RaisingFloorPanelRowReader,
)
from tests.nodes.node_floor_page_api_effect.support import document_event
from tests.nodes.node_floor_panel_answer_effect.support import (
    ADDRESS,
    DEADLINE_S,
    T0,
    command,
    handler,
)

pytestmark = pytest.mark.unit

OLD_T0 = datetime(2026, 11, 8, 23, 30, tzinfo=UTC)


class BlockingReader:
    """A row reader that never answers."""

    async def read(self, key: str) -> None:
        await asyncio.Event().wait()


def old_route() -> str:
    store = InMemoryFloorDocumentStore()
    store.put_if_newer(document_event(OLD_T0))
    return (
        HandlerFloorPage(store)
        .handle(ModelFloorPageRequest(document="floor_simple"))
        .model_dump_json()
    )


async def old_route_beside(
    panel: Callable[[], Awaitable[object]], *, coupled: bool
) -> str:
    """The old route's answer, with the new panel handler run beside it (or inside the same call when coupled)."""

    async def served() -> str:
        if coupled:
            await (
                panel()
            )  # one dispatch: the panel's failure or delay is the old route's
        return old_route()

    side = None if coupled else asyncio.ensure_future(panel())
    try:
        return await asyncio.wait_for(served(), timeout=DEADLINE_S)
    finally:
        if side is not None:
            side.cancel()
            await asyncio.gather(side, return_exceptions=True)


async def problems_isolation(reader: object, *, coupled: bool) -> list[str]:
    baseline = old_route()
    panel_handler = handler(reader, T0)  # type: ignore[arg-type]

    async def panel() -> object:
        return await panel_handler.handle(command(ADDRESS))

    try:
        answer = await old_route_beside(panel, coupled=coupled)
    except Exception as exc:
        return [f"the old route failed beside the panel: {type(exc).__name__}"]
    return (
        []
        if answer == baseline
        else ["the old route's answer differs from the run without the panel"]
    )


async def test_panel_isolation_from_the_old_route() -> None:
    assert await problems_isolation(RaisingFloorPanelRowReader(), coupled=False) == []
    assert await problems_isolation(InMemoryFloorPanelRowReader(), coupled=False) == []
    assert await problems_isolation(BlockingReader(), coupled=False) == []


async def test_control_isolation_coupled_routes_propagate_a_failure() -> None:
    """Injected break `coupled_routes`: the panel's handler is inside the old route's call and its exception propagates."""

    async def propagating() -> list[str]:
        async def panel() -> object:
            raise RuntimeError("panel handler raised through the runtime")

        try:
            await old_route_beside(panel, coupled=True)
        except Exception as exc:
            return [f"the old route failed beside the panel: {type(exc).__name__}"]
        return []

    assert await propagating() != []


async def test_control_isolation_a_blocking_panel_misses_the_old_routes_deadline() -> (
    None
):
    """Injected break `blocking_panel_handler_in_the_same_call`."""
    assert await problems_isolation(BlockingReader(), coupled=True) != []
