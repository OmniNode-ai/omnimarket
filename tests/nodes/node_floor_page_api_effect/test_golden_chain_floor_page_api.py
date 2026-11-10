# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains of the floor route handler and its mounted app (OMN-23072)."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from omnimarket.handlers.floor_document_store import InMemoryFloorDocumentStore
from omnimarket.nodes.node_floor_page_api_effect.handlers.handler_floor_page import (
    HandlerFloorPage,
    contract_routes,
)
from omnimarket.nodes.node_floor_page_api_effect.handlers.http_app import create_app
from omnimarket.nodes.node_floor_page_api_effect.models.model_floor_page_request import (
    ModelFloorPageRequest,
)
from tests.nodes.node_floor_page_api_effect.support import document_event

pytestmark = pytest.mark.unit
T0 = datetime(2026, 11, 8, 23, 30, tzinfo=UTC)


def test_golden_the_held_document_is_the_body() -> None:
    store = InMemoryFloorDocumentStore()
    event = document_event(T0)
    store.put_if_newer(event)
    answer = HandlerFloorPage(store).handle(
        ModelFloorPageRequest(document="floor_simple")
    )
    assert answer.status_code == 200
    assert answer.body == event.document.model_dump(mode="json")
    assert answer.computed_at == "2026-11-08T23:30:00Z"


def test_error_nothing_projected_refuses_503_with_the_contract_code() -> None:
    answer = HandlerFloorPage(InMemoryFloorDocumentStore()).handle(
        ModelFloorPageRequest(document="floor_simple")
    )
    assert (answer.status_code, answer.body["code"]) == (
        503,
        "floor.document.not_projected",
    )


def test_error_an_undeclared_document_refuses_404() -> None:
    answer = HandlerFloorPage(InMemoryFloorDocumentStore()).handle(
        ModelFloorPageRequest(document="compare")
    )
    assert (answer.status_code, answer.body["code"]) == (404, "floor.document.unknown")


async def test_golden_the_app_serves_every_route_the_contract_declares() -> None:
    store = InMemoryFloorDocumentStore()
    store.put_if_newer(document_event(T0))
    transport = httpx.ASGITransport(app=create_app(store=store))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://floor"
    ) as client:
        for route in contract_routes():
            response = await client.get(f"/floor{route.path}")
            assert response.status_code == 200
            assert response.headers["X-Floor-Computed-At"] == "2026-11-08T23:30:00Z"
        assert (await client.get("/floor/not-declared.json")).status_code == 404
