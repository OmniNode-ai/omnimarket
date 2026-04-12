# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for AdapterLinearFill epic/parent ticket filtering.

Related: OMN-7773 — build loop classifier should filter epic-type tickets
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from omnimarket.nodes.node_build_dispatch_effect.handlers.dispatch_history_store import (
    DispatchHistoryStore,
)
from omnimarket.nodes.node_build_loop_orchestrator.handlers.adapter_linear_fill import (
    AdapterLinearFill,
)


def _clean_adapter(**kwargs: Any) -> AdapterLinearFill:
    """Return an AdapterLinearFill with an isolated (empty) history store."""
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        tmp = Path(f.name)
    tmp.unlink()  # remove so store treats it as missing (empty)
    return AdapterLinearFill(history_store=DispatchHistoryStore(path=tmp), **kwargs)


def _issue(
    identifier: str,
    *,
    children: int = 0,
    labels: tuple[str, ...] = (),
    priority: int = 2,
) -> dict[str, Any]:
    return {
        "id": f"uuid-{identifier}",
        "identifier": identifier,
        "title": f"Title for {identifier}",
        "description": "body",
        "priority": priority,
        "state": {"name": "Backlog"},
        "labels": {"nodes": [{"name": lbl} for lbl in labels]},
        "children": {"nodes": [{"id": f"child-{i}"} for i in range(children)]},
    }


def _graphql_response(issues: list[dict[str, Any]]) -> dict[str, Any]:
    return {"data": {"issues": {"nodes": issues}}}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_leaf_tickets_pass_through() -> None:
    adapter = _clean_adapter(api_key="fake", team_id="team-1")
    payload = _graphql_response(
        [_issue("OMN-1001"), _issue("OMN-1002")],
    )

    with patch("httpx.AsyncClient.post") as mock_post:
        mock_post.return_value = AsyncMock(
            status_code=200,
            json=lambda: payload,
            raise_for_status=lambda: None,
        )
        result = await adapter.handle(correlation_id=uuid4(), max_tickets=5)

    ids = [t.ticket_id for t in result.selected_tickets]
    assert ids == ["OMN-1001", "OMN-1002"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tickets_with_children_are_filtered_out() -> None:
    adapter = _clean_adapter(api_key="fake", team_id="team-1")
    payload = _graphql_response(
        [
            _issue("OMN-7727", children=5),  # epic
            _issue("OMN-2000"),  # leaf
            _issue("OMN-7741", children=3),  # epic
        ],
    )

    with patch("httpx.AsyncClient.post") as mock_post:
        mock_post.return_value = AsyncMock(
            status_code=200,
            json=lambda: payload,
            raise_for_status=lambda: None,
        )
        result = await adapter.handle(correlation_id=uuid4(), max_tickets=5)

    ids = [t.ticket_id for t in result.selected_tickets]
    assert ids == ["OMN-2000"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tickets_with_epic_label_are_filtered_out() -> None:
    adapter = _clean_adapter(api_key="fake", team_id="team-1")
    payload = _graphql_response(
        [
            _issue("OMN-3001", labels=("epic",)),
            _issue("OMN-3002", labels=("Epic",)),  # case-insensitive
            _issue("OMN-3003", labels=("bug",)),  # not an epic label
            _issue("OMN-3004", labels=("meta",)),
        ],
    )

    with patch("httpx.AsyncClient.post") as mock_post:
        mock_post.return_value = AsyncMock(
            status_code=200,
            json=lambda: payload,
            raise_for_status=lambda: None,
        )
        result = await adapter.handle(correlation_id=uuid4(), max_tickets=5)

    ids = [t.ticket_id for t in result.selected_tickets]
    assert ids == ["OMN-3003"]
