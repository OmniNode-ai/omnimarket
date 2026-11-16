# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fixtures of the piece 9 landing reading tests (OMN-22918): an overlay with the landing piece, a
fixed clock and snapshots of the pull request watcher. Every number is synthetic."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from omnimarket.models.model_reading import (
    ModelLandingHead,
    ModelLandingSnapshot,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)
from omnimarket.nodes.node_reading_writer_effect.handlers.handler_reading_writer import (
    HandlerReadingWriter,
)
from omnimarket.nodes.node_reading_writer_effect.overlay import ModelReadingOverlay
from omnimarket.nodes.node_reading_writer_effect.protocols import ProtocolLandingSource
from tests.nodes.node_reading_writer_effect.reading_support import (
    RUN_AT,
    START,
    overlay_document,
    stamp,
)

HEAD_LIMIT = 3600
GOAL = 10
WAITING_THRESHOLD = 10


def landing_overlay_document() -> dict[str, Any]:
    """The test overlay with piece 9 readable from the snapshot of real landing alone."""
    document = overlay_document()
    document["order"] = [1, 6, 9, 15]
    document["pieces"].append(
        {
            "piece": 9,
            "name": "landing",
            "readable_today": True,
            "basis": "landing-goal-hourly-requirements",
            "landing": {
                "head_max_age_seconds": HEAD_LIMIT,
                "goal_merges_per_window": GOAL,
                "merge_window_seconds": 3600,
                "waiting_threshold": WAITING_THRESHOLD,
                "snapshot_max_age_seconds": 900,
            },
            "injected_fault": "hold one eligible head for over an hour in a test repository",
        }
    )
    document["landing_route"] = {
        "watcher_state_env": "READING_TEST_WATCHER_STATE",
        "goal_scope_overlay_id": "goal_scope",
        "goal_scope_file_env": "READING_TEST_GOAL_SCOPE",
        "queue_classes": ["green-unarmed", "queue-stuck"],
        "conflict_merge_states": ["DIRTY"],
    }
    return document


def landing_overlay() -> ModelReadingOverlay:
    return ModelReadingOverlay.model_validate(landing_overlay_document())


def heads(count: int, *, waiting_seconds: int = 600) -> tuple[ModelLandingHead, ...]:
    return tuple(
        ModelLandingHead(ref=f"repo#{n}", waiting_seconds=waiting_seconds)
        for n in range(1, count + 1)
    )


def snapshot(
    *,
    waiting: tuple[ModelLandingHead, ...] = (),
    merges: int = 0,
    age_seconds: int = 60,
    newest_at: datetime | None = None,
) -> ModelLandingSnapshot:
    extra: dict[str, Any] = {} if newest_at is None else {"newest_at": newest_at}
    return ModelLandingSnapshot(
        age_seconds=age_seconds, heads=waiting, merges_in_window=merges, **extra
    )


def canary_row(*, landed: bool = True, at_offset_hours: int = 5) -> str:
    """A row of the retired synthetic canary. The reading must not read it (RULING 2026-11-08T14:38:40Z)."""
    return (
        f"{stamp(START - timedelta(hours=at_offset_hours))} | STATUS | lane=landing-canary"
        f" | principal=dev-test | actor=script:landing-canary | model=none"
        f" | outcome={'landed' if landed else 'not-landed'} | req=canary-{at_offset_hours}"
        " | via=bus:h200 | synthetic landing canary"
    )


def writer(
    ledger: InMemoryLedger, landing: ProtocolLandingSource | None, *, at=RUN_AT
) -> HandlerReadingWriter:
    return HandlerReadingWriter(
        overlay=landing_overlay(),
        source=ledger,
        appender=ledger,
        clock=lambda: at,
        landing=landing,
    )
