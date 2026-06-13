"""Golden-chain tests for node_pipeline_fill."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml

from omnimarket.nodes.node_pipeline_fill.handlers.handler_pipeline_fill import (
    HandlerPipelineFill,
)
from omnimarket.nodes.node_pipeline_fill.models.model_pipeline_fill_command import (
    ModelPipelineFillCommand,
)


class _FakeLinearClient:
    async def list_active_sprint_unstarted(self) -> list[dict[str, Any]]:
        return [
            {
                "id": "OMN-9001",
                "identifier": "OMN-9001",
                "title": "Repair merge sweep coverage",
                "priority": 1,
                "state": {"name": "Backlog"},
                "labels": {"nodes": [{"name": "s"}]},
                "description": "",
                "relations": {"nodes": []},
                "createdAt": "2026-01-01T00:00:00Z",
            }
        ]


class _FakeEventBus:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, Any]]] = []

    async def publish(self, *, topic: str, payload: dict[str, Any]) -> None:
        self.published.append((topic, payload))


@pytest.mark.unit
@pytest.mark.asyncio
async def test_pipeline_fill_golden_chain_dispatches_and_records_state(
    tmp_path: Path,
) -> None:
    """Command -> Linear candidates -> RSD score -> ticket-pipeline event -> state."""
    event_bus = _FakeEventBus()
    command = ModelPipelineFillCommand(
        correlation_id=uuid.uuid4(),
        top_n=1,
        wave_cap=2,
        min_score=0.1,
        dry_run=False,
        state_dir=str(tmp_path),
    )
    handler = HandlerPipelineFill(
        linear_client=_FakeLinearClient(),
        event_bus=event_bus,
    )

    with patch(
        "omnimarket.nodes.node_pipeline_fill.handlers.handler_pipeline_fill._resolve_omni_home",
        return_value=tmp_path,
    ):
        result = await handler.handle(command)

    assert result.candidates_found == 1
    assert result.candidates_after_filter == 1
    assert result.dispatched == ("OMN-9001",)
    assert event_bus.published == [
        (
            "onex.cmd.omnimarket.ticket-pipeline-start.v1",
            {
                "ticket_id": "OMN-9001",
                "correlation_id": str(command.correlation_id),
                "triggered_by": "node_pipeline_fill",
            },
        )
    ]

    dispatched = yaml.safe_load((tmp_path / "dispatched.yaml").read_text())
    assert dispatched["in_flight"][0]["ticket_id"] == "OMN-9001"
    assert dispatched["in_flight"][0]["status"] == "running"

    last_run = yaml.safe_load((tmp_path / "last-run.yaml").read_text())
    assert last_run["dispatched"] == ["OMN-9001"]
    assert last_run["dry_run"] is False
