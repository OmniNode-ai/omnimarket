# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared builders of the host drift owner dispatch tests: the example overlay, items, a fixed clock and an in-memory handler."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml

from omnimarket.nodes.node_host_drift_owner_dispatch_effect.adapters.inmemory_adapters import (
    InMemoryBriefPreparer,
    InMemoryDispatchRecord,
    InMemoryDriftOpenItems,
    InMemoryLanePlacer,
    InMemoryLiveLanes,
)
from omnimarket.nodes.node_host_drift_owner_dispatch_effect.handlers.handler_host_drift_owner_dispatch import (
    HandlerHostDriftOwnerDispatch,
)
from omnimarket.nodes.node_host_drift_owner_dispatch_effect.models import (
    ModelDriftOpenItem,
)
from omnimarket.nodes.node_host_drift_owner_dispatch_effect.overlay import (
    ModelDriftOwnerOverlay,
)

NODE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_host_drift_owner_dispatch_effect"
)
NOW = datetime(2026, 11, 13, 16, 0, 0, tzinfo=UTC)
RUNNER_PATH = "/example/plugin/remote-lane/onex_remote_lane.py"
CLAUDE_DIR = "/example/home/lane-claude"


def overlay() -> ModelDriftOwnerOverlay:
    return ModelDriftOwnerOverlay.model_validate(
        yaml.safe_load((NODE / "overlay.example.yaml").read_text(encoding="utf-8"))
    )


def item(
    key: str = "env-file:host-env",
    *,
    host: str = "h-example",
    kind: str = "env-file",
    first_seen_at: str = "2026-11-07T00:01:37Z",
    observed: str = "a file holds 1 secret names",
) -> ModelDriftOpenItem:
    return ModelDriftOpenItem(
        host=host,
        key=key,
        kind=kind,
        declared="no secret name beyond the bootstrap names",
        observed=observed,
        first_seen_at=first_seen_at,
        bound_seconds=3600,
        verdict="DRIFT",
    )


@dataclass
class Rig:
    handler: HandlerHostDriftOwnerDispatch
    items: InMemoryDriftOpenItems
    live: InMemoryLiveLanes
    record: InMemoryDispatchRecord
    briefs: InMemoryBriefPreparer
    runner: InMemoryLanePlacer


def rig(
    items: tuple[ModelDriftOpenItem, ...],
    *,
    covered_causes: tuple[str, ...] = (),
    covered_lanes: tuple[str, ...] = (),
    record: dict[str, str] | None = None,
    changed: ModelDriftOwnerOverlay | None = None,
) -> Rig:
    source = InMemoryDriftOpenItems("h-example", items)
    live = InMemoryLiveLanes(covered_causes, covered_lanes)
    kept = InMemoryDispatchRecord(record)
    briefs = InMemoryBriefPreparer()
    runner = InMemoryLanePlacer(briefs)
    used = changed or overlay()
    handler = HandlerHostDriftOwnerDispatch(
        overlay=used,
        items=source,
        live=live,
        record=kept,
        briefs=briefs,
        runner=runner,
        runner_path=RUNNER_PATH,
        claude_dir=CLAUDE_DIR if used.dispatch.claude_dir_env else "",
        clock=lambda: NOW,
    )
    return Rig(handler, source, live, kept, briefs, runner)
