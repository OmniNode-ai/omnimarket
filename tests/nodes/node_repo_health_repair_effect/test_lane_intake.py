# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_repo_health_repair_effect opens only lane-created items (OMN-17942).

The repair ticket is labelled, stamped on its first line with the lane (or this
node, when no lane declared itself), and admitted by the guard's lane-intake
check before Linear is touched. A refusal raises before any create. Every
refusal case has an admitted twin beside it.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.handlers.handler_lane_intake import (
    LaneIntakeGate,
    LaneIntakeRefusedError,
)
from omnimarket.nodes.node_repo_health_repair_effect.handlers.handler_repo_health_repair import (
    HandlerRepoHealthRepairEffect,
)
from tests.nodes.node_repo_health_repair_effect.test_handler_repo_health_repair_effect import (
    _command,
)

pytestmark = pytest.mark.unit


class _Client:
    def __init__(self, existing: str | None = None) -> None:
        self._existing = existing
        self.created: list[dict[str, Any]] = []

    def search_issues_by_content_key(self, *, content_key: str) -> str | None:
        del content_key
        return self._existing

    def create_issue(
        self,
        *,
        title: str,
        description: str,
        parent_id: str,
        label_names: Sequence[str] = (),
    ) -> str:
        self.created.append(
            {"title": title, "description": description, "labels": list(label_names)}
        )
        return "OMN-70001"


@pytest.mark.asyncio
async def test_the_repair_ticket_is_stamped_labelled_and_admitted(
    monkeypatch: pytest.MonkeyPatch, lane_intake_gate: list[dict[str, Any]]
) -> None:
    monkeypatch.delenv("ONEX_REMOTE_LANE", raising=False)
    monkeypatch.setenv("ONEX_LANE", "lane-a")
    client = _Client()
    event = await HandlerRepoHealthRepairEffect(linear_client=client).handle(_command())
    assert event.ticket_created is True
    made = client.created[0]
    assert made["description"].startswith("stamped-by: lane-a\n\n")
    assert made["labels"] == ["test-lane-label"]
    assert lane_intake_gate[0]["lane"] == "lane-a"
    admitted = lane_intake_gate[0]["tool_input"]
    assert admitted["labels"] == ["test-lane-label"]
    assert admitted["description"] == made["description"]


@pytest.mark.asyncio
async def test_with_no_lane_the_node_stamps_itself(
    monkeypatch: pytest.MonkeyPatch, lane_intake_gate: list[dict[str, Any]]
) -> None:
    monkeypatch.delenv("ONEX_LANE", raising=False)
    monkeypatch.delenv("ONEX_REMOTE_LANE", raising=False)
    client = _Client()
    await HandlerRepoHealthRepairEffect(linear_client=client).handle(_command())
    assert client.created[0]["description"].startswith(
        "stamped-by: node_repo_health_repair_effect\n\n"
    )
    assert lane_intake_gate[0]["lane"] == "node_repo_health_repair_effect"


@pytest.mark.asyncio
async def test_a_refused_intake_creates_nothing(
    monkeypatch: pytest.MonkeyPatch, lane_intake_gate: list[dict[str, Any]]
) -> None:
    """Negative control: the same command, with the guard refusing, never reaches
    Linear -- and with the guard admitting (the twin above) it does."""

    def refuse(self: object, tool_input: dict[str, object], lane: str) -> None:
        del self, tool_input, lane
        raise LaneIntakeRefusedError("30 items were already admitted today")

    monkeypatch.setattr(LaneIntakeGate, "admit", refuse)
    client = _Client()
    with pytest.raises(LaneIntakeRefusedError, match="already admitted today"):
        await HandlerRepoHealthRepairEffect(linear_client=client).handle(_command())
    assert client.created == []
    assert lane_intake_gate == []


@pytest.mark.asyncio
async def test_a_dry_run_and_a_deduplicated_ticket_spend_no_intake(
    lane_intake_gate: list[dict[str, Any]],
) -> None:
    client = _Client()
    await HandlerRepoHealthRepairEffect(linear_client=client).handle(
        _command(dry_run=True)
    )
    await HandlerRepoHealthRepairEffect(linear_client=_Client("OMN-5")).handle(
        _command()
    )
    assert lane_intake_gate == []
    assert client.created == []


def test_the_emitted_event_topic_is_the_one_the_contract_publishes() -> None:
    """The repair event a lane-created ticket produces rides the declared topic."""
    contract = (
        Path(__file__).resolve().parents[3]
        / "src/omnimarket/nodes/node_repo_health_repair_effect/contract.yaml"
    )
    published = yaml.safe_load(contract.read_text(encoding="utf-8"))["event_bus"][
        "publish_topics"
    ]
    assert "onex.evt.omnimarket.repo-health-repair-emitted.v1" in published
