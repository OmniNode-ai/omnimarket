# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden-chain coverage for node_repo_health_repair_effect."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest

from omnimarket.events.repo_health import (
    EnumFailureOrigin,
    ModelRepoHealthClassification,
)
from omnimarket.nodes.node_repo_health_repair_effect.handlers.handler_repo_health_repair import (
    HandlerRepoHealthRepairEffect,
)
from omnimarket.nodes.node_repo_health_repair_effect.models.model_repair_command import (
    ModelRepoHealthRepairCommand,
)


class _RecordingLinearClient:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []

    def search_issues_by_content_key(self, *, content_key: str) -> str | None:
        return None

    def create_issue(self, *, title: str, description: str, parent_id: str) -> str:
        self.created.append(
            {"title": title, "description": description, "parent_id": parent_id}
        )
        return "OMN-99001"


def _command() -> ModelRepoHealthRepairCommand:
    classification = ModelRepoHealthClassification(
        origin=EnumFailureOrigin.REPO_BASELINE,
        reason="pre-commit failure is present on the dev baseline",
        matched_paths=("src/omnimarket/legacy.py", "tests/test_legacy.py"),
        correlation_id=UUID("00000000-0000-4000-a000-000000000135"),
        repo="OmniNode-ai/omnimarket",
        pr_number=1415,
        failing_command="pre-commit run --all-files",
    )
    return ModelRepoHealthRepairCommand(
        correlation_id=classification.correlation_id,
        classification=classification,
        parent_issue_id="OMN-13316",
        dry_run=False,
    )


@pytest.mark.asyncio
async def test_golden_chain_repo_baseline_repair_emits_linear_ticket() -> None:
    client = _RecordingLinearClient()
    command = _command()

    event = await HandlerRepoHealthRepairEffect(linear_client=client).handle(command)

    assert event.ticket_created is True
    assert event.repair_ticket_ref == "OMN-99001"
    assert event.repo == "OmniNode-ai/omnimarket"
    assert event.pr_number == 1415
    assert event.failing_command == "pre-commit run --all-files"
    assert event.classification_reason == command.classification.reason
    assert event.content_key

    assert len(client.created) == 1
    created = client.created[0]
    assert created["parent_id"] == "OMN-13316"
    assert "pre-commit run --all-files" in created["description"]
    assert "src/omnimarket/legacy.py" in created["description"]
    assert event.content_key in created["title"]
