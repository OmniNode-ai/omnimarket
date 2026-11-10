# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared builders: the policy comes from a tmp overlay, as the node reads it in production."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import yaml

from omnimarket.nodes.node_dev_roll_request_compute.models import (
    EnumDevRollHealth,
    ModelDevRollFacts,
    ModelDevRollPolicy,
    ModelDevRollRecord,
    ModelDevTipChanged,
)
from omnimarket.nodes.node_dev_roll_request_compute.overlay import (
    NODE_NAME,
    load_dev_roll_policy,
)

NOW = datetime(2026, 11, 10, 12, 0, 0, tzinfo=UTC)
POLICY_DOC: dict[str, object] = {
    "mode": "shadow",
    "batch_window_seconds": 1800,
    "min_start_interval_seconds": 1800,
    "healthy_for_seconds": 600,
    "failed_roll_release_after_seconds": 3600,
    "known_repositories": ["omnimarket", "omnibase_infra"],
    "build_context_repository": "omnibase_infra",
    "fallback_git_ref": "origin/dev",
    "scope": "full",
    "runtime_lane": "dev",
    "requested_by": "test-requester",
}


def write_overlay(root: Path, doc: dict[str, object] | None = None) -> dict[str, str]:
    """Write ``<root>/<node>/overlay.yaml`` and return the environ that points at it."""
    node_dir = root / NODE_NAME
    node_dir.mkdir(parents=True, exist_ok=True)
    (node_dir / "overlay.yaml").write_text(
        yaml.safe_dump(POLICY_DOC if doc is None else doc), encoding="utf-8"
    )
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(root)}


@pytest.fixture
def policy(tmp_path: Path) -> ModelDevRollPolicy:
    return load_dev_roll_policy(write_overlay(tmp_path))


def tip(
    repository: str = "omnimarket",
    *,
    age_seconds: int,
    sha: str | None = None,
    event_id: UUID | None = None,
) -> ModelDevTipChanged:
    """A tip event merged ``age_seconds`` before NOW."""
    return ModelDevTipChanged(
        event_id=event_id or uuid4(),
        repository=repository,
        sha=sha or uuid4().hex + "00000000",
        merged_at=NOW - timedelta(seconds=age_seconds),
    )


def roll(
    *,
    started_ago: int | None = 7200,
    finished_ago: int | None = 6000,
    health: EnumDevRollHealth = EnumDevRollHealth.HEALTHY,
    healthy_ago: int | None = 6000,
) -> ModelDevRollRecord:
    return ModelDevRollRecord(
        job_id="job-1",
        git_ref="b" * 40,
        runtime_started_at=None
        if started_ago is None
        else NOW - timedelta(seconds=started_ago),
        finished_at=None
        if finished_ago is None
        else NOW - timedelta(seconds=finished_ago),
        health=health,
        healthy_since=None
        if healthy_ago is None
        else NOW - timedelta(seconds=healthy_ago),
    )


def facts(
    events: tuple[ModelDevTipChanged, ...], last: ModelDevRollRecord | None = None
) -> ModelDevRollFacts:
    return ModelDevRollFacts(
        correlation_id=uuid4(), observed_at=NOW, pending_events=events, last_roll=last
    )
