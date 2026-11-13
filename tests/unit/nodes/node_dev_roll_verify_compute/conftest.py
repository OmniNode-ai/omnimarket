# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared builders: the policy comes from a tmp overlay, as the node reads it in production."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import yaml

from omnimarket.nodes.node_dev_roll_verify_compute.models import (
    EnumDevStartCause,
    ModelDevBranchInstall,
    ModelDevRollVerifyFacts,
    ModelDevRollVerifyPolicy,
    ModelDevRuntimeStarted,
)
from omnimarket.nodes.node_dev_roll_verify_compute.overlay import (
    NODE_NAME,
    load_dev_roll_verify_policy,
)

NOW = datetime(2026, 11, 12, 12, 0, 0, tzinfo=UTC)
ROLL_START = NOW - timedelta(seconds=7200)
ROLL_FINISH = NOW - timedelta(seconds=6000)
POLICY_DOC: dict[str, object] = {
    "declared_starts_per_roll": 1,
    "runtime_lane": "dev",
}
# One hot patch declared inside the roll's one start, as the build manifest lists it.
DECLARED_INSTALLS: tuple[ModelDevBranchInstall, ...] = (
    ModelDevBranchInstall(repository="omnimarket", branch="dev", sha="a" * 40),
)


def write_overlay(root: Path, doc: dict[str, object] | None = None) -> dict[str, str]:
    """Write ``<root>/<node>/overlay.yaml`` and return the environ that points at it."""
    node_dir = root / NODE_NAME
    node_dir.mkdir(parents=True, exist_ok=True)
    (node_dir / "overlay.yaml").write_text(
        yaml.safe_dump(POLICY_DOC if doc is None else doc), encoding="utf-8"
    )
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(root)}


@pytest.fixture
def policy(tmp_path: Path) -> ModelDevRollVerifyPolicy:
    return load_dev_roll_verify_policy(write_overlay(tmp_path))


def start(
    job_id: str = "job-1",
    seconds_after_roll_start: int = 60,
    cause: EnumDevStartCause = EnumDevStartCause.ROLL_RECREATE,
    event_id: UUID | None = None,
) -> ModelDevRuntimeStarted:
    """A dev_runtime_started event ``seconds_after_roll_start`` into the roll."""
    return ModelDevRuntimeStarted(
        event_id=event_id or uuid4(),
        job_id=job_id,
        runtime_lane="dev",
        started_at=ROLL_START + timedelta(seconds=seconds_after_roll_start),
        cause=cause,
    )


def facts(
    starts: tuple[ModelDevRuntimeStarted, ...] | None,
    *,
    job_id: str = "job-1",
    roll_finished_at: datetime | None = ROLL_FINISH,
) -> ModelDevRollVerifyFacts:
    """One verification tick's facts: the roll finished before NOW with its starts."""
    return ModelDevRollVerifyFacts(
        correlation_id=uuid4(),
        observed_at=NOW,
        job_id=job_id,
        roll_finished_at=roll_finished_at,
        starts=None if starts is None else tuple(starts),
        declared_branch_installs=DECLARED_INSTALLS,
    )
