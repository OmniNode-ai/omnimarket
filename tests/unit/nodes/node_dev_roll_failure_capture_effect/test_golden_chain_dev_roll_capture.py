"""Golden chain test for node_dev_roll_failure_capture_effect."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from omnimarket.nodes.node_dev_roll_failure_capture_effect.adapters.adapter_in_memory import (
    InMemoryRollArtifactStore,
)
from omnimarket.nodes.node_dev_roll_failure_capture_effect.models import (
    ModelDevRollIncident,
    ModelDevRollManifest,
)
from tests.unit.nodes.node_dev_roll_failure_capture_effect.chain_support import (
    RuntimeDispatch,
    InMemoryTransport,
    build_routing_map,
    discover_contracts_from_paths,
    make_handler_resolver,
)

pytestmark = pytest.mark.unit

CONTRACT_PATH = Path(
    "src/omnimarket/nodes/node_dev_roll_failure_capture_effect/contract.yaml"
)

INCIDENT_TOPIC = "onex.evt.omnimarket.dev-roll-incident.v1"
MANIFEST_TOPIC = "onex.evt.omnimarket.dev-roll-manifest.v1"
TERMINAL_TOPIC = "onex.evt.omnimarket.dev-roll-terminal-recorded.v1"
FAILED_TOPIC = "onex.evt.omnimarket.dev-roll-capture-failed.v1"
MANIFEST_RECORDED_TOPIC = "onex.evt.omnimarket.dev-roll-manifest-recorded.v1"


def _incident_payload() -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "job_id": "job-1",
        "issuer": "deploy-agent",
        "commit": "abc123",
        "error": "container exited",
        "project": "omnimarket",
        "at": datetime.now(timezone.utc).isoformat(),
        "restored_revision": "rev-1",
        "recovery_verified": True,
        "recovery_skipped_healthy": None,
        "recovery_error": None,
        "runtime_kept_revision": None,
        "health_probes": [],
        "log_captures": [
            {
                "service": "api",
                "container": "api-1",
                "bytes_total": 100,
                "bytes_kept": 100,
                "complete": True,
                "redacted": False,
                "sha256": "a" * 64,
                "artifact_path": "job-1/api.log",
                "error": None,
                "log": "hello world",
            }
        ],
    }


def _manifest_payload() -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "job_id": "job-1",
        "issuer": "deploy-agent",
        "git_ref": "main",
        "scope": "full",
        "requested_at": None,
        "accepted_at": datetime.now(timezone.utc).isoformat(),
        "open_pr": "123",
        "rollback": False,
    }


@pytest.mark.asyncio
async def test_golden_incident_chain() -> None:
    store = InMemoryRollArtifactStore()
    contracts = discover_contracts_from_paths([CONTRACT_PATH])
    routing = build_routing_map(contracts, make_handler_resolver(store))
    transport = InMemoryTransport()
    dispatch = RuntimeDispatch(routing, transport)

    payload = _incident_payload()
    await dispatch.dispatch(INCIDENT_TOPIC, payload)

    terminal_events = [e for e in transport.published if e["topic"] == TERMINAL_TOPIC]
    failed_events = [e for e in transport.published if e["topic"] == FAILED_TOPIC]
    assert len(terminal_events) == 1
    assert len(failed_events) == 0
    assert "job-1/api.log" in store._store


@pytest.mark.asyncio
async def test_golden_manifest_chain() -> None:
    store = InMemoryRollArtifactStore()
    contracts = discover_contracts_from_paths([CONTRACT_PATH])
    routing = build_routing_map(contracts, make_handler_resolver(store))
    transport = InMemoryTransport()
    dispatch = RuntimeDispatch(routing, transport)

    payload = _manifest_payload()
    await dispatch.dispatch(MANIFEST_TOPIC, payload)

    manifest_events = [e for e in transport.published if e["topic"] == MANIFEST_RECORDED_TOPIC]
    assert len(manifest_events) == 1
