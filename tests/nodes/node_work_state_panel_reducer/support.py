# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared builders of the work-state panel reducer's tests (OMN-19998)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from omnimarket.models.work_state_panel import (
    ModelWorkStatePanelEntry,
    ModelWorkStatePanelOverlay,
)
from omnimarket.models.work_state_projection.model_work_state_projected_event import (
    EnumWorkStateProjectorProfile,
    ModelWorkStateProjectedEvent,
)

T0 = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
NOW = T0 + timedelta(seconds=300)
MARKER = "MARKER-7f3a"
# The topic is the node's own declaration: the contract's one subscribed topic.
CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_work_state_panel_reducer/contract.yaml"
)
TOPIC: str = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))["event_bus"][
    "subscribe_topics"
][0]


def entry(**override: Any) -> ModelWorkStatePanelEntry:
    base: dict[str, Any] = {
        "panel_id": "work_state_latest_run",
        "projector": EnumWorkStateProjectorProfile.FILE_SOURCES,
        "topic": TOPIC,
        "max_age_s": 900,
        "max_future_skew_s": 60,
        "accept_deadline_s": 600,
        "audience": "team_service",
        "allowed_readers": ("reader-a",),
        "answer_writer": "writer-a",
        "reducer_group": "panel-reducer",
        "start_position": "latest",
    }
    return ModelWorkStatePanelEntry(**(base | override))


def overlay() -> ModelWorkStatePanelOverlay:
    return ModelWorkStatePanelOverlay(panels=(entry(),))


def event(as_of_s: int = 0, **override: Any) -> ModelWorkStateProjectedEvent:
    base: dict[str, Any] = {
        "computed_at": T0,
        "tick_id": "t1",
        "projector": EnumWorkStateProjectorProfile.FILE_SOURCES,
        "as_of": T0 + timedelta(seconds=as_of_s),
        "item_count": 3,
        "counts": {"available": 2, "done": 1},
        "ready_in_scope": 2,
        "unread": (),
        "stale_sources": (),
    }
    return ModelWorkStateProjectedEvent(**(base | override))
