# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains of the floor document projection (OMN-23072)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from omnimarket.handlers.floor_document_store import (
    InMemoryFloorDocumentStore,
    get_floor_document_store,
    reset_floor_document_store,
)
from omnimarket.nodes.node_floor_document_reducer.handlers.handler_floor_document_reducer import (
    HandlerFloorDocumentReducer,
)
from tests.nodes.node_floor_page_api_effect.support import document_event

pytestmark = pytest.mark.unit
T0 = datetime(2026, 11, 8, 23, 30, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _fresh_store():
    reset_floor_document_store()
    yield
    reset_floor_document_store()


def test_golden_the_first_document_is_held_as_the_latest() -> None:
    applied = HandlerFloorDocumentReducer().handle(document_event(T0))
    assert applied.applied is True
    assert applied.held_computed_at == T0
    assert get_floor_document_store().latest() is not None


def test_golden_a_newer_document_replaces_the_held_one() -> None:
    h = HandlerFloorDocumentReducer()
    h.handle(document_event(T0))
    newer = T0 + timedelta(minutes=1)
    assert h.handle(document_event(newer)).held_computed_at == newer


def test_error_an_older_document_is_refused_and_the_held_one_stays() -> None:
    h = HandlerFloorDocumentReducer()
    h.handle(document_event(T0))
    refused = h.handle(document_event(T0 - timedelta(minutes=1)))
    assert refused.applied is False
    assert refused.held_computed_at == T0
    assert "not newer" in refused.reason


def test_error_a_redelivery_of_the_same_document_converges() -> None:
    store = InMemoryFloorDocumentStore()
    h = HandlerFloorDocumentReducer(store)
    assert h.handle(document_event(T0)).applied is True
    assert h.handle(document_event(T0)).applied is False
    held = store.latest()
    assert held is not None
    assert held.computed_at == T0
