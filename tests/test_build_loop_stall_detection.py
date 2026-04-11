# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for build loop stall detection and dispatch history store.

Related: OMN-7774 — build loop stall detection
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from omnimarket.nodes.node_build_dispatch_effect.handlers.dispatch_history_store import (
    MAX_DISPATCH_ATTEMPTS,
    STALL_WINDOW_MINUTES,
    DispatchHistoryStore,
)
from omnimarket.nodes.node_build_loop_orchestrator.handlers.adapter_linear_fill import (
    AdapterLinearFill,
)


@pytest.fixture
def store(tmp_path: Path) -> DispatchHistoryStore:
    return DispatchHistoryStore(path=tmp_path / "history.json")


@pytest.mark.unit
def test_empty_store_has_no_records(store: DispatchHistoryStore) -> None:
    assert store.load() == {}
    assert store.should_skip("OMN-1") is False
    assert store.is_stalled("OMN-1") is False


@pytest.mark.unit
def test_record_dispatch_creates_new_entry(store: DispatchHistoryStore) -> None:
    cid = "corr-1"
    record = store.record_dispatch("OMN-100", correlation_id=cid)
    assert record.attempt_count == 1
    assert record.first_dispatched_at == record.last_dispatched_at
    assert record.last_correlation_id == cid

    reloaded = store.load()
    assert "OMN-100" in reloaded
    assert reloaded["OMN-100"].attempt_count == 1


@pytest.mark.unit
def test_record_dispatch_increments_attempts(store: DispatchHistoryStore) -> None:
    first = store.record_dispatch("OMN-200", correlation_id="c1")
    second = store.record_dispatch("OMN-200", correlation_id="c2")
    assert second.attempt_count == 2
    assert second.first_dispatched_at == first.first_dispatched_at
    assert second.last_correlation_id == "c2"


@pytest.mark.unit
def test_should_skip_after_max_attempts(store: DispatchHistoryStore) -> None:
    for i in range(MAX_DISPATCH_ATTEMPTS):
        store.record_dispatch("OMN-300", correlation_id=f"c{i}")
    assert store.should_skip("OMN-300") is True
    assert store.should_skip("OMN-999") is False


@pytest.mark.unit
def test_is_stalled_when_past_window(store: DispatchHistoryStore) -> None:
    past = datetime.now(tz=UTC) - timedelta(minutes=STALL_WINDOW_MINUTES + 1)
    store.record_dispatch("OMN-400", correlation_id="c", now=past)
    assert store.is_stalled("OMN-400") is True


@pytest.mark.unit
def test_is_not_stalled_within_window(store: DispatchHistoryStore) -> None:
    store.record_dispatch("OMN-500", correlation_id="c")
    assert store.is_stalled("OMN-500") is False


@pytest.mark.unit
def test_clear_ticket_resets_history(store: DispatchHistoryStore) -> None:
    store.record_dispatch("OMN-600", correlation_id="c")
    store.clear_ticket("OMN-600")
    assert store.load() == {}
    assert store.should_skip("OMN-600") is False


def _issue(identifier: str) -> dict[str, Any]:
    return {
        "id": f"uuid-{identifier}",
        "identifier": identifier,
        "title": f"Title for {identifier}",
        "description": "body",
        "priority": 2,
        "state": {"name": "Backlog"},
        "labels": {"nodes": []},
        "children": {"nodes": []},
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fill_adapter_skips_tickets_at_max_attempts(
    store: DispatchHistoryStore,
) -> None:
    # Seed the store with a ticket that has already hit the max attempt count
    for i in range(MAX_DISPATCH_ATTEMPTS):
        store.record_dispatch("OMN-7742", correlation_id=f"c{i}")

    adapter = AdapterLinearFill(
        api_key="fake",
        team_id="team-1",
        history_store=store,
    )
    payload = {"data": {"issues": {"nodes": [_issue("OMN-7742"), _issue("OMN-9999")]}}}

    with patch("httpx.AsyncClient.post") as mock_post:
        mock_post.return_value = AsyncMock(
            status_code=200,
            json=lambda: payload,
            raise_for_status=lambda: None,
        )
        result = await adapter.handle(correlation_id=uuid4(), max_tickets=5)

    ids = [t.ticket_id for t in result.selected_tickets]
    assert ids == ["OMN-9999"]
