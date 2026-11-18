# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared builders: the overlay, the two payloads, and the two handlers over one store."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml

from omnimarket.nodes.node_dev_roll_failure_capture_effect.handlers import (
    HandlerDevRollIncidentCapture,
    HandlerDevRollManifestRecord,
)
from omnimarket.nodes.node_dev_roll_failure_capture_effect.handlers.store_dev_roll_artifact import (
    InMemoryDevRollArtifactStore,
)
from omnimarket.nodes.node_dev_roll_failure_capture_effect.models import (
    ModelDevRollCapturePolicy,
    ModelDevRollInMemoryStore,
)
from omnimarket.nodes.node_dev_roll_failure_capture_effect.overlay import (
    NODE_NAME,
    load_capture_policy,
)

NOW = datetime(2026, 11, 12, 12, 0, 0, tzinfo=UTC)
MAX_LOG_BYTES = 1024
# The policy the handlers are given with an injected store (the overlay supplies it in production).
POLICY = ModelDevRollCapturePolicy(
    store=ModelDevRollInMemoryStore(backend="in_memory"), max_log_bytes=MAX_LOG_BYTES
)
_SHA = "a" * 64
LOG_CAPTURE: dict[str, Any] = {
    "service": "omnimarket",
    "container": "omnimarket-dev",
    "bytes_total": 2048,
    "bytes_kept": 1024,
    "complete": True,
    "redacted": False,
    "sha256": _SHA,
    "artifact_path": "dev-roll-capture/logs/omnimarket-dev.log",
    "error": None,
    "log": "boot guard recovered the failed roll",
}
OVERLAY_DOC: dict[str, Any] = {
    "store": {
        "backend": "file",
        "home_env": "EXAMPLE_STATE_DIR",
        "relpath": "dev-roll-capture",
    },
    "max_log_bytes": 1024,
}


def incident_payload(**overrides: Any) -> dict[str, object]:
    """A valid incident exactly as the producer publishes it."""
    payload: dict[str, object] = {
        "event_id": str(uuid4()),
        "job_id": "job-1",
        "issuer": "operator-lane",
        "commit": "b" * 40,
        "error": "boot guard recovered a failed roll",
        "project": "omnibase-infra",
        "at": NOW.isoformat(),
        "restored_revision": "c" * 40,
        "recovery_verified": True,
        "recovery_skipped_healthy": None,
        "recovery_error": None,
        "runtime_kept_revision": None,
        "health_probes": [{"probe": "health", "ok": True}],
        "log_captures": [dict(LOG_CAPTURE)],
    }
    payload.update(overrides)
    return payload


def manifest_payload(**overrides: Any) -> dict[str, object]:
    """A valid manifest exactly as the producer publishes it."""
    payload: dict[str, object] = {
        "event_id": str(uuid4()),
        "job_id": "job-1",
        "issuer": "operator-lane",
        "git_ref": "d" * 40,
        "scope": "runtime",
        "requested_at": NOW.isoformat(),
        "accepted_at": NOW.isoformat(),
        "open_pr": "",
        "rollback": False,
    }
    payload.update(overrides)
    return payload


def write_overlay(root: Path, doc: dict[str, object] | None = None) -> dict[str, str]:
    """Write ``<root>/<node>/overlay.yaml`` and return the environ that points at it."""
    node_dir = root / NODE_NAME
    node_dir.mkdir(parents=True, exist_ok=True)
    (node_dir / "overlay.yaml").write_text(
        yaml.safe_dump(OVERLAY_DOC if doc is None else doc), encoding="utf-8"
    )
    return {
        "ONEX_SKILL_OVERLAY_ROOTS": str(root),
        "EXAMPLE_STATE_DIR": str(root / "state"),
    }


@pytest.fixture(autouse=True)
def _overlay_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the overlay env vars at a valid overlay so handlers can read it."""
    env = write_overlay(tmp_path)
    for key, value in env.items():
        monkeypatch.setenv(key, value)


@pytest.fixture
def store() -> InMemoryDevRollArtifactStore:
    return InMemoryDevRollArtifactStore()


def handlers(
    store: InMemoryDevRollArtifactStore,
    clock: datetime = NOW,
) -> tuple[HandlerDevRollManifestRecord, HandlerDevRollIncidentCapture]:
    """The two handlers over one shared store with a fixed clock."""
    return (
        HandlerDevRollManifestRecord(store, policy=POLICY),
        HandlerDevRollIncidentCapture(store, policy=POLICY, clock=lambda: clock),
    )


@pytest.fixture
def policy(tmp_path: Path):
    return load_capture_policy(write_overlay(tmp_path))
