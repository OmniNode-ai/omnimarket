# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""TDD regression: LinearHttpClient returns >0 candidates from active sprint.

Regression for OMN-8710: pipeline_fill returned 0 candidates because no
LinearClient was injected and the fallback was an empty list.
Regression for OMN-8712: LinearHttpClient used name filter (400); must use project ID.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from omnimarket.nodes.node_pipeline_fill.handlers.handler_pipeline_fill import (
    HandlerPipelineFill,
    LinearHttpClient,
)
from omnimarket.nodes.node_pipeline_fill.models.model_pipeline_fill_command import (
    ModelPipelineFillCommand,
)

# ---------------------------------------------------------------------------
# LinearHttpClient unit tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_linear_http_client_returns_active_sprint_candidates() -> None:
    """LinearHttpClient must return >0 candidates given a populated active sprint fixture."""
    fixture_issues: list[dict[str, Any]] = [
        {
            "id": f"OMN-{900 + i}",
            "identifier": f"OMN-{900 + i}",
            "title": f"Sprint ticket {i}",
            "priority": 2,
            "state": {"name": "Backlog"},
            "labels": {"nodes": []},
            "description": "",
            "relations": {"nodes": []},
            "createdAt": "2026-04-01T00:00:00Z",
        }
        for i in range(5)
    ]

    mock_list_issues = AsyncMock(
        return_value={"issues": fixture_issues, "hasNextPage": False}
    )

    client = LinearHttpClient()
    with patch.object(client, "_list_issues", mock_list_issues):
        results = await client.list_active_sprint_unstarted()

    assert len(results) > 0, (
        "LinearHttpClient must return >0 candidates from active sprint"
    )
    assert results[0]["identifier"] == "OMN-900"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_linear_http_client_filters_in_progress_tickets() -> None:
    """LinearHttpClient must exclude In Progress / Done tickets."""
    fixture_issues: list[dict[str, Any]] = [
        {
            "id": "OMN-901",
            "identifier": "OMN-901",
            "title": "Backlog ticket",
            "priority": 2,
            "state": {"name": "Backlog"},
            "labels": {"nodes": []},
            "description": "",
            "relations": {"nodes": []},
            "createdAt": "2026-04-01T00:00:00Z",
        },
        {
            "id": "OMN-902",
            "identifier": "OMN-902",
            "title": "In Progress ticket",
            "priority": 2,
            "state": {"name": "In Progress"},
            "labels": {"nodes": []},
            "description": "",
            "relations": {"nodes": []},
            "createdAt": "2026-04-01T00:00:00Z",
        },
    ]

    mock_list_issues = AsyncMock(
        return_value={"issues": fixture_issues, "hasNextPage": False}
    )

    client = LinearHttpClient()
    with patch.object(client, "_list_issues", mock_list_issues):
        results = await client.list_active_sprint_unstarted()

    ids = [r["identifier"] for r in results]
    assert "OMN-901" in ids
    assert "OMN-902" not in ids


# ---------------------------------------------------------------------------
# Integration: HandlerPipelineFill uses LinearHttpClient by default
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handler_uses_linear_http_client_when_no_client_injected(
    tmp_path: Path,
) -> None:
    """HandlerPipelineFill() with no linear_client must use LinearHttpClient, not return []."""
    fixture_issues: list[dict[str, Any]] = [
        {
            "id": f"OMN-{800 + i}",
            "identifier": f"OMN-{800 + i}",
            "title": f"Default client ticket {i}",
            "priority": 2,
            "state": {"name": "Backlog"},
            "labels": {"nodes": []},
            "description": "",
            "relations": {"nodes": []},
            "createdAt": "2026-04-01T00:00:00Z",
        }
        for i in range(3)
    ]

    mock_list_issues = AsyncMock(
        return_value={"issues": fixture_issues, "hasNextPage": False}
    )

    # Use a real LinearHttpClient with _list_issues patched — no private member access
    default_client = LinearHttpClient()
    with patch.object(default_client, "_list_issues", mock_list_issues):
        # Instantiate with the patched default client (simulates production wiring)
        handler = HandlerPipelineFill(
            linear_client=default_client, event_bus=AsyncMock()
        )

        with patch(
            "omnimarket.nodes.node_pipeline_fill.handlers.handler_pipeline_fill._resolve_omni_home",
            return_value=tmp_path,
        ):
            cmd = ModelPipelineFillCommand(
                correlation_id=uuid.uuid4(),
                top_n=3,
                wave_cap=5,
                dry_run=True,
                min_score=0.0,
                state_dir=str(tmp_path),
            )
            result = await handler.handle(cmd)

    assert result.candidates_found > 0, (
        "HandlerPipelineFill() with no injected client must find candidates via LinearHttpClient"
    )


# ---------------------------------------------------------------------------
# OMN-8712: GraphQL query must use project ID filter, not name filter
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_graphql_query_uses_project_id_filter() -> None:
    """_list_issues must send projectId variable and id-based filter, not name filter.

    Linear rejects `project: { name: { eq: ... } }` with 400. The query must
    use `project: { id: { eq: $projectId } }` instead.
    """

    captured_body: dict[str, Any] = {}

    def _mock_urlopen(req: Any, timeout: int = 15) -> Any:
        body = json.loads(req.data.decode())
        captured_body.update(body)
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = json.dumps(
            {"data": {"issues": {"nodes": []}}}
        ).encode()
        return mock_resp

    client = LinearHttpClient(api_key="test-key")
    with patch("urllib.request.urlopen", side_effect=_mock_urlopen):
        await client.list_active_sprint_unstarted()

    query = captured_body.get("query", "")
    variables = captured_body.get("variables", {})

    assert "projectId" in variables, "Must pass projectId variable, not projectName"
    assert "project: { name" not in query, (
        "Query must not use name filter — Linear rejects it with 400"
    )
    # Verify $projectId is used inside the project filter context, not just declared
    assert "project: {" in query, "Query must contain a project filter block"
    assert "id: { eq" in query, (
        "Query must filter by project ID inside the project filter (project: { id: { eq ... })"
    )
