# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for HandlerPipelineFill.

Related:
    - OMN-8688: node_pipeline_fill
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from omnimarket.nodes.node_pipeline_fill.handlers.handler_pipeline_fill import (
    HandlerPipelineFill,
    _is_blocked,
    _to_scored_ticket,
)
from omnimarket.nodes.node_pipeline_fill.models.model_pipeline_fill_command import (
    ModelPipelineFillCommand,
)


def _make_ticket(
    ticket_id: str = "OMN-100",
    title: str = "Fix foo",
    priority: int = 2,
    state: str = "Backlog",
    labels: list[str] | None = None,
    blocked: bool = False,
) -> dict[str, Any]:
    label_nodes = [{"name": lbl} for lbl in (labels or [])]
    if blocked:
        label_nodes.append({"name": "blocked"})
    return {
        "id": ticket_id,
        "identifier": ticket_id,
        "title": title,
        "priority": priority,
        "state": {"name": state},
        "labels": {"nodes": label_nodes},
        "description": "",
        "relations": {"nodes": []},
        "createdAt": "2026-01-01T00:00:00Z",
    }


def _make_command(
    top_n: int = 5,
    wave_cap: int = 5,
    dry_run: bool = False,
    min_score: float = 0.0,
    tmp_path: Path | None = None,
) -> ModelPipelineFillCommand:
    return ModelPipelineFillCommand(
        correlation_id=uuid.uuid4(),
        top_n=top_n,
        wave_cap=wave_cap,
        dry_run=dry_run,
        min_score=min_score,
        state_dir=str(tmp_path) if tmp_path else ".onex_state/pipeline-fill",
    )


# ---------------------------------------------------------------------------
# Unit tests — pure helpers
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_is_blocked_via_label() -> None:
    ticket = _make_ticket(blocked=True)
    assert _is_blocked(ticket) is True


@pytest.mark.unit
def test_is_blocked_not_blocked() -> None:
    ticket = _make_ticket()
    assert _is_blocked(ticket) is False


@pytest.mark.unit
def test_to_scored_ticket_fields() -> None:
    raw = _make_ticket("OMN-200", "Add tests", priority=1, labels=["s"])
    scored = _to_scored_ticket(raw)
    assert scored.ticket_id == "OMN-200"
    assert scored.title == "Add tests"
    assert scored.priority == 1
    assert "s" in scored.labels
    assert 0.0 <= scored.rsd_score <= 1.0


@pytest.mark.unit
def test_to_scored_ticket_high_priority_scores_higher() -> None:
    urgent = _to_scored_ticket(_make_ticket(priority=1))
    low = _to_scored_ticket(_make_ticket(priority=4))
    assert urgent.rsd_score > low.rsd_score


# ---------------------------------------------------------------------------
# Handler tests — mock Linear client + event bus
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dispatch_top_n(tmp_path: Path) -> None:
    tickets = [_make_ticket(f"OMN-{i}", f"Ticket {i}", priority=2) for i in range(8)]

    linear_client = AsyncMock()
    linear_client.list_active_sprint_unstarted.return_value = tickets

    event_bus = AsyncMock()

    handler = HandlerPipelineFill(linear_client=linear_client, event_bus=event_bus)

    cmd = _make_command(top_n=3, wave_cap=5, tmp_path=tmp_path)
    with patch(
        "omnimarket.nodes.node_pipeline_fill.handlers.handler_pipeline_fill._resolve_omni_home",
        return_value=tmp_path,
    ):
        result = await handler.handle(cmd)

    assert len(result.dispatched) == 3
    assert event_bus.publish.call_count == 3


@pytest.mark.unit
@pytest.mark.asyncio
async def test_wave_cap_blocks_dispatch(tmp_path: Path) -> None:
    import yaml as _yaml

    tickets = [_make_ticket(f"OMN-{i}") for i in range(5)]

    linear_client = AsyncMock()
    linear_client.list_active_sprint_unstarted.return_value = tickets

    # Pre-populate dispatched.yaml with 5 in-flight (equal to wave_cap=5)
    dispatched_path = tmp_path / "dispatched.yaml"
    dispatched_path.write_text(
        _yaml.dump(
            {
                "in_flight": [
                    {"ticket_id": f"OMN-{100 + i}", "status": "running"}
                    for i in range(5)
                ]
            }
        )
    )

    handler = HandlerPipelineFill(linear_client=linear_client, event_bus=AsyncMock())
    cmd = _make_command(wave_cap=5, tmp_path=tmp_path)
    with patch(
        "omnimarket.nodes.node_pipeline_fill.handlers.handler_pipeline_fill._resolve_omni_home",
        return_value=tmp_path,
    ):
        result = await handler.handle(cmd)

    assert result.candidates_found == 0
    assert "Wave cap" in result.skip_reason
    assert len(result.dispatched) == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_blocked_tickets_filtered(tmp_path: Path) -> None:
    tickets = [
        _make_ticket("OMN-1", blocked=False),
        _make_ticket("OMN-2", blocked=True),
        _make_ticket("OMN-3", blocked=False),
    ]

    linear_client = AsyncMock()
    linear_client.list_active_sprint_unstarted.return_value = tickets
    event_bus = AsyncMock()

    handler = HandlerPipelineFill(linear_client=linear_client, event_bus=event_bus)
    cmd = _make_command(top_n=5, tmp_path=tmp_path)
    with patch(
        "omnimarket.nodes.node_pipeline_fill.handlers.handler_pipeline_fill._resolve_omni_home",
        return_value=tmp_path,
    ):
        result = await handler.handle(cmd)

    assert result.candidates_found == 3
    assert result.candidates_after_filter == 2
    assert "OMN-2" not in result.dispatched


@pytest.mark.unit
@pytest.mark.asyncio
async def test_already_in_flight_filtered(tmp_path: Path) -> None:
    import yaml as _yaml

    dispatched_path = tmp_path / "dispatched.yaml"
    dispatched_path.write_text(
        _yaml.dump({"in_flight": [{"ticket_id": "OMN-10", "status": "running"}]})
    )

    tickets = [_make_ticket("OMN-10"), _make_ticket("OMN-20")]
    linear_client = AsyncMock()
    linear_client.list_active_sprint_unstarted.return_value = tickets
    event_bus = AsyncMock()

    handler = HandlerPipelineFill(linear_client=linear_client, event_bus=event_bus)
    cmd = _make_command(top_n=5, tmp_path=tmp_path)
    with patch(
        "omnimarket.nodes.node_pipeline_fill.handlers.handler_pipeline_fill._resolve_omni_home",
        return_value=tmp_path,
    ):
        result = await handler.handle(cmd)

    assert "OMN-10" not in result.dispatched
    assert "OMN-20" in result.dispatched


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dry_run_no_dispatch(tmp_path: Path) -> None:
    tickets = [_make_ticket(f"OMN-{i}") for i in range(3)]
    linear_client = AsyncMock()
    linear_client.list_active_sprint_unstarted.return_value = tickets
    event_bus = AsyncMock()

    handler = HandlerPipelineFill(linear_client=linear_client, event_bus=event_bus)
    cmd = _make_command(dry_run=True, top_n=3, tmp_path=tmp_path)
    with patch(
        "omnimarket.nodes.node_pipeline_fill.handlers.handler_pipeline_fill._resolve_omni_home",
        return_value=tmp_path,
    ):
        result = await handler.handle(cmd)

    # In dry_run, dispatched IDs are populated but event_bus.publish is never called
    assert result.dry_run is True
    assert len(result.dispatched) == 3
    event_bus.publish.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_min_score_filters_low_scoring_tickets(tmp_path: Path) -> None:
    # Create a ticket that will have low RSD score (low priority, recent)
    tickets = [
        {
            "id": "OMN-50",
            "identifier": "OMN-50",
            "title": "Low priority ticket",
            "priority": 4,  # Low
            "state": {"name": "Backlog"},
            "labels": {"nodes": [{"name": "xl"}]},  # XL = size_score 0.1
            "description": "",
            "relations": {"nodes": []},
            "createdAt": "2026-04-12T00:00:00Z",  # Very recent
        }
    ]
    linear_client = AsyncMock()
    linear_client.list_active_sprint_unstarted.return_value = tickets
    event_bus = AsyncMock()

    handler = HandlerPipelineFill(linear_client=linear_client, event_bus=event_bus)
    cmd = _make_command(min_score=0.9, tmp_path=tmp_path)  # Very high threshold
    with patch(
        "omnimarket.nodes.node_pipeline_fill.handlers.handler_pipeline_fill._resolve_omni_home",
        return_value=tmp_path,
    ):
        result = await handler.handle(cmd)

    assert len(result.dispatched) == 0
    assert "min_score" in result.skip_reason


@pytest.mark.unit
@pytest.mark.asyncio
async def test_state_file_written_after_dispatch(tmp_path: Path) -> None:
    import yaml as _yaml

    tickets = [_make_ticket("OMN-99")]
    linear_client = AsyncMock()
    linear_client.list_active_sprint_unstarted.return_value = tickets
    event_bus = AsyncMock()

    handler = HandlerPipelineFill(linear_client=linear_client, event_bus=event_bus)
    cmd = _make_command(top_n=1, tmp_path=tmp_path)
    with patch(
        "omnimarket.nodes.node_pipeline_fill.handlers.handler_pipeline_fill._resolve_omni_home",
        return_value=tmp_path,
    ):
        await handler.handle(cmd)

    dispatched_path = tmp_path / "dispatched.yaml"
    assert dispatched_path.exists()
    state = _yaml.safe_load(dispatched_path.read_text())
    in_flight_ids = [e["ticket_id"] for e in state.get("in_flight", [])]
    assert "OMN-99" in in_flight_ids
