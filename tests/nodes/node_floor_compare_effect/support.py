# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared builders of the comparison tests: a registry home with a served floor, and dated snapshots."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from omnimarket.nodes.node_floor_compare_effect.handlers.handler_floor_compare import (
    load_runtime,
)
from omnimarket.nodes.node_floor_compare_effect.handlers.handler_floor_history import (
    append_snapshot,
)
from omnimarket.nodes.node_floor_compare_effect.models.model_floor_compare import (
    ModelSnapshot,
)

FIXTURE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_factory_floor_publish_effect/tests/fixtures/floor_simple_served_2026-11-06T1838Z.json"
)
RT = load_runtime()
NOW = datetime(2026, 11, 7, 18, 30, tzinfo=UTC)


def served_floor(at: datetime = NOW) -> dict:
    doc = json.loads(FIXTURE.read_text(encoding="utf-8"))
    doc["generated_at"] = at.isoformat().replace("+00:00", "Z")
    return doc


def build_home(tmp: Path, doc: dict | None = None) -> Path:
    home = tmp / "home"
    path = home / RT.simple_relpath
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(doc or served_floor()), encoding="utf-8")
    return home


def snap(
    days_ago: float, values: dict[str, float], source: str = "tick", now: datetime = NOW
) -> ModelSnapshot:
    return ModelSnapshot(
        at=now - timedelta(days=days_ago), source=source, values=values
    )


def seed(home: Path, *snapshots: ModelSnapshot) -> None:
    for s in snapshots:
        append_snapshot(home / RT.history_relpath, s)
