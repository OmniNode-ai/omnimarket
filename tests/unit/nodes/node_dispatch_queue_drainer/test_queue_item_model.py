# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for legacy dispatch queue item validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_dispatch_queue_drainer.models import ModelDispatchQueueItem


@pytest.mark.unit
def test_queue_item_converts_to_dispatch_worker_command() -> None:
    item = ModelDispatchQueueItem(
        name="omn-9437-fixer",
        team="Omninode",
        role="fixer",
        scope="Compile queued OMN-9437 work",
        targets=["OMN-9437", "omnimarket#444"],
        repo="omnimarket",
        collision_fences=["omnimarket#443 (owned by other-worker)"],
    )

    command = item.to_dispatch_worker_command()

    assert command.name == "omn-9437-fixer"
    assert command.role == "fixer"
    assert command.targets == ["OMN-9437", "omnimarket#444"]
    assert command.collision_fences == ["omnimarket#443 (owned by other-worker)"]


@pytest.mark.unit
def test_queue_item_infers_repo_from_repo_target() -> None:
    item = ModelDispatchQueueItem(
        name="omn-9437-fixer",
        team="Omninode",
        role="fixer",
        scope="Compile queued OMN-9437 work",
        targets=["OMN-9437", "omnimarket#444"],
    )

    assert item.resolved_repo == "omnimarket"


@pytest.mark.unit
def test_queue_item_rejects_missing_targets() -> None:
    with pytest.raises(ValidationError, match="targets must contain"):
        ModelDispatchQueueItem(
            name="omn-9437-fixer",
            team="Omninode",
            role="fixer",
            scope="Compile queued OMN-9437 work",
            targets=[],
        )
