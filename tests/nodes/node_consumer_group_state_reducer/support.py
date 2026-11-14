# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Builders shared by the consumer group state reducer tests (OMN-23992)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import yaml

from omnimarket.nodes.node_consumer_group_state_reducer.handlers.handler_group_state import (
    fold,
)
from omnimarket.nodes.node_consumer_group_state_reducer.models import (
    ModelGroupCensusEvent,
    ModelGroupStateRecord,
)
from omnimarket.nodes.node_consumer_group_state_reducer.overlay import (
    ModelGroupStateOverlay,
)

NODE_DIR = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_consumer_group_state_reducer"
)
START = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)
LANE = "lane-a"
DUE_A = "example.client.0123456789ab"
DUE_B = "example.client.ba9876543210"
STABLE = "example.stable-group"
DRIFT = "example.client.host_drift_fleet.h053"
PROTECTED_LOOKALIKE = "host_drift_fleet.h053"


def overlay() -> ModelGroupStateOverlay:
    document = yaml.safe_load((NODE_DIR / "overlay.example.yaml").read_text())
    return ModelGroupStateOverlay.model_validate(document)


def census(
    groups: dict[str, str],
    *,
    seconds: int = 0,
    lane: str = LANE,
    correlation_id: UUID | None = None,
) -> ModelGroupCensusEvent:
    return ModelGroupCensusEvent(
        correlation_id=correlation_id or uuid4(),
        bus_lane=lane,
        observed_at=START + timedelta(seconds=seconds),
        groups=groups,
    )


def fold_all(
    events: list[ModelGroupCensusEvent],
    prior: ModelGroupStateRecord | None = None,
    bound: ModelGroupStateOverlay | None = None,
) -> ModelGroupStateRecord:
    record = prior
    for item in events:
        record = fold(record, item, bound or overlay())
    assert record is not None
    return record
