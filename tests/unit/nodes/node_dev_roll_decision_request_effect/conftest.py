# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared builders: the policy, the manifest payload and the handlers over one in-memory store."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest

from omnimarket.nodes.node_dev_roll_decision_request_effect.handlers.store_dev_roll_observation import (
    InMemoryDevRollObservationStore,
)
from omnimarket.nodes.node_dev_roll_decision_request_effect.models import (
    ModelDevRollDecisionRequestPolicy,
    ModelDevRollInMemoryObservationStore,
)

NOW = datetime(2026, 11, 10, 12, 0, 0, tzinfo=UTC)
SHA = "a" * 40
SHADOW_ROLL_SECONDS = 300
# The policy the handlers are given with an injected store (the overlay supplies it in production).
POLICY = ModelDevRollDecisionRequestPolicy(
    mode="shadow",
    issuer_repository_pattern=r"^gha/(?P<repository>[a-z0-9_]+)/pr-[0-9]+$",
    shadow_roll_duration_seconds=SHADOW_ROLL_SECONDS,
    seen_job_ids_kept=50,
    store=ModelDevRollInMemoryObservationStore(backend="in_memory"),
)
OVERLAY_DOC: dict[str, Any] = {
    "mode": "shadow",
    "issuer_repository_pattern": r"^gha/(?P<repository>[a-z0-9_]+)/pr-[0-9]+$",
    "shadow_roll_duration_seconds": SHADOW_ROLL_SECONDS,
    "seen_job_ids_kept": 50,
    "store": {
        "backend": "file",
        "home_env": "EXAMPLE_STATE_DIR",
        "relpath": "dev-roll-decision-request",
    },
}


def manifest_payload(
    *, age_seconds: int = 0, sha: str = SHA, **overrides: Any
) -> dict[str, object]:
    """One manifest as the deploy agent publishes it, requested ``age_seconds`` before NOW."""
    requested = NOW - timedelta(seconds=age_seconds)
    payload: dict[str, object] = {
        "event_id": str(uuid4()),
        "job_id": str(uuid4()),
        "issuer": "gha/omnimarket/pr-12",
        "git_ref": sha,
        "scope": "full",
        "requested_at": requested.isoformat(),
        "accepted_at": (requested + timedelta(seconds=5)).isoformat(),
        "open_pr": "",
        "rollback": False,
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def store() -> InMemoryDevRollObservationStore:
    return InMemoryDevRollObservationStore()
