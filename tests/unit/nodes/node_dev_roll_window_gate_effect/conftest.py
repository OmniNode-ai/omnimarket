# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared builders: the policy, a dev deploy command, a fixed clock and the three handlers over one store."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from omnimarket.events.runtime_deployment import (
    EnumRedeployStatus,
    EnumRuntimeLane,
    ModelDeployPublishCommand,
    ModelDeployRebuildCompleted,
)
from omnimarket.nodes.node_dev_roll_window_gate_effect.handlers import (
    HandlerDevRollWindowCompleted,
    HandlerDevRollWindowHold,
    HandlerDevRollWindowTick,
    InMemoryDevRollGateStore,
)
from omnimarket.nodes.node_dev_roll_window_gate_effect.models import (
    ModelDevRollGateInMemoryStore,
    ModelDevRollWindowGatePolicy,
)

NOW = datetime(2026, 11, 14, 12, 0, 0, tzinfo=UTC)
WINDOW = 900
MIN_INTERVAL = 900
HEALTHY_FOR = 600
FAILED_RELEASE = 3600
HELD_KEPT = 50
ISSUER_PATTERN = r"^gha/(?P<repository>[a-z0-9_]+)/pr-[0-9]+$"
POLICY = ModelDevRollWindowGatePolicy(
    mode="live",
    issuer_repository_pattern=ISSUER_PATTERN,
    batch_window_seconds=WINDOW,
    min_start_interval_seconds=MIN_INTERVAL,
    healthy_for_seconds=HEALTHY_FOR,
    failed_roll_release_after_seconds=FAILED_RELEASE,
    known_repositories=("omnimarket", "omnibase_infra"),
    build_context_repository="omnibase_infra",
    fallback_git_ref="origin/dev",
    requested_by="dev-roll-window-gate",
    held_kept=HELD_KEPT,
    store=ModelDevRollGateInMemoryStore(backend="in_memory"),
)
OVERLAY_DOC: dict[str, Any] = {
    "mode": "live",
    "issuer_repository_pattern": ISSUER_PATTERN,
    "batch_window_seconds": WINDOW,
    "min_start_interval_seconds": MIN_INTERVAL,
    "healthy_for_seconds": HEALTHY_FOR,
    "failed_roll_release_after_seconds": FAILED_RELEASE,
    "known_repositories": ["omnimarket", "omnibase_infra"],
    "build_context_repository": "omnibase_infra",
    "fallback_git_ref": "origin/dev",
    "requested_by": "dev-roll-window-gate",
    "held_kept": HELD_KEPT,
    "store": {
        "backend": "file",
        "home_env": "EXAMPLE_STATE_DIR",
        "relpath": "dev-roll-window-gate",
    },
}


class Clock:
    """A settable clock; tests move it, nothing sleeps."""

    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now = self.now + timedelta(seconds=seconds)


def sha(number: int) -> str:
    return f"{number:040x}"


def command(
    *,
    number: int = 1,
    age_seconds: int = 0,
    requested_by: str = "gha/omnimarket/pr-12",
    lane: EnumRuntimeLane = EnumRuntimeLane.DEV,
    git_ref: str | None = None,
    correlation_id: UUID | None = None,
    **overrides: Any,
) -> ModelDeployPublishCommand:
    """One dev deploy command requested ``age_seconds`` before NOW."""
    return ModelDeployPublishCommand(
        correlation_id=correlation_id or uuid4(),
        git_ref=sha(number) if git_ref is None else git_ref,
        runtime_lane=lane,
        requested_by=requested_by,
        requested_at=NOW - timedelta(seconds=age_seconds),
        **overrides,
    )


def completed(
    command_id: object,
    *,
    at: datetime,
    status: EnumRedeployStatus = EnumRedeployStatus.SUCCESS,
) -> ModelDeployRebuildCompleted:
    return ModelDeployRebuildCompleted(
        correlation_id=str(command_id), completed_at=at, status=status
    )


class Gate:
    """The three handlers over one in-memory store and one clock."""

    def __init__(
        self,
        policy: ModelDevRollWindowGatePolicy = POLICY,
        clock: Clock | None = None,
    ) -> None:
        self.clock = clock or Clock()
        self.store = InMemoryDevRollGateStore()
        self.hold = HandlerDevRollWindowHold(
            self.store, policy=policy, clock=self.clock
        )
        self.tick = HandlerDevRollWindowTick(
            self.store, policy=policy, clock=self.clock
        )
        self.completed = HandlerDevRollWindowCompleted(
            self.store, policy=policy, clock=self.clock
        )


@pytest.fixture
def gate() -> Gate:
    return Gate()
