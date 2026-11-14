# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared builders: the gate policy, the batching policy from a tmp overlay, commands and a clock."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import yaml

from omnimarket.events.runtime_deployment import (
    EnumRuntimeLane,
    ModelDeployPublishCommand,
)
from omnimarket.nodes.node_dev_roll_gate_effect.handlers.store_dev_roll_gate import (
    InMemoryDevRollGateStore,
)
from omnimarket.nodes.node_dev_roll_gate_effect.models import (
    ModelDevRollGateInMemoryStore,
    ModelDevRollGatePolicy,
)
from omnimarket.nodes.node_dev_roll_request_compute.handlers import (
    HandlerDevRollRequest,
)
from omnimarket.nodes.node_dev_roll_request_compute.models import ModelDevRollPolicy
from omnimarket.nodes.node_dev_roll_request_compute.overlay import (
    NODE_NAME as COMPUTE_NODE_NAME,
)
from omnimarket.nodes.node_dev_roll_request_compute.overlay import (
    load_dev_roll_policy,
)

NOW = datetime(2026, 11, 14, 12, 0, 0, tzinfo=UTC)
ISSUER = r"^gha/(?P<repository>[a-z0-9_]+)/pr-[0-9]+$"
ROLL_BOUND_SECONDS = 3600
WINDOW_SECONDS = 600
MIN_INTERVAL_SECONDS = 1800
HEALTHY_FOR_SECONDS = 60
FAILED_RELEASE_SECONDS = 1200

GATE_DOC: dict[str, object] = {
    "mode": "live",
    "issuer_repository_pattern": ISSUER,
    "unmatched_repository": "omnimarket",
    "roll_unfinished_after_seconds": ROLL_BOUND_SECONDS,
    "ids_kept": 50,
    "store": {"backend": "in_memory"},
}
COMPUTE_DOC: dict[str, object] = {
    "mode": "shadow",
    "batch_window_seconds": WINDOW_SECONDS,
    "min_start_interval_seconds": MIN_INTERVAL_SECONDS,
    "healthy_for_seconds": HEALTHY_FOR_SECONDS,
    "failed_roll_release_after_seconds": FAILED_RELEASE_SECONDS,
    "known_repositories": ["omnimarket", "omnibase_infra"],
    "build_context_repository": "omnimarket",
    "fallback_git_ref": "origin/dev",
    "scope": "full",
    "runtime_lane": "dev",
    "requested_by": "test-requester",
}


def gate_policy(**overrides: object) -> ModelDevRollGatePolicy:
    return ModelDevRollGatePolicy.model_validate({**GATE_DOC, **overrides})


POLICY = gate_policy()
assert isinstance(POLICY.store, ModelDevRollGateInMemoryStore)


def write_compute_overlay(root: Path, **overrides: object) -> dict[str, str]:
    node_dir = root / COMPUTE_NODE_NAME
    node_dir.mkdir(parents=True, exist_ok=True)
    (node_dir / "overlay.yaml").write_text(
        yaml.safe_dump({**COMPUTE_DOC, **overrides}), encoding="utf-8"
    )
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(root)}


class Clock:
    """A settable clock; the handlers and the chain read it, nothing else does."""

    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def command(
    *,
    repository: str = "omnimarket",
    sha: str | None = None,
    correlation_id: UUID | None = None,
) -> ModelDeployPublishCommand:
    """One dev deploy command as the CI trigger sends it."""
    return ModelDeployPublishCommand(
        correlation_id=correlation_id or uuid4(),
        runtime_lane=EnumRuntimeLane.DEV,
        git_ref=sha if sha is not None else uuid4().hex + "00000000",
        requested_by=f"gha/{repository}/pr-12",
        requested_at=NOW,
    )


@pytest.fixture
def store() -> InMemoryDevRollGateStore:
    return InMemoryDevRollGateStore()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def compute_policy(tmp_path: Path) -> ModelDevRollPolicy:
    return load_dev_roll_policy(write_compute_overlay(tmp_path))


@pytest.fixture
def compute(compute_policy: ModelDevRollPolicy) -> HandlerDevRollRequest:
    return HandlerDevRollRequest(compute_policy)
