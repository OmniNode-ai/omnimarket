# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam: the incident and manifest events this node captures, against their producers.

The deploy agent's publisher lives in omnibase_infra (the other repository), which cannot be
imported here, and a skipped cross-repository test is refused. So this side SPELLS the wire
shape; the infra side spells the same sets in tests/unit/handlers/test_handler_dev_roll_events_omn24013.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_dev_roll_failure_capture_effect.handlers import (
    HandlerDevRollIncidentCapture,
)
from omnimarket.nodes.node_dev_roll_failure_capture_effect.handlers.store_dev_roll_artifact import (
    InMemoryDevRollArtifactStore,
)
from omnimarket.nodes.node_dev_roll_failure_capture_effect.models import (
    EnumDevRollOutcome,
    ModelDevRollIncidentEvent,
    ModelDevRollManifestEvent,
    ModelDevRollTerminal,
)
from omnimarket.nodes.node_dev_roll_failure_capture_effect.models.model_dev_roll_log_capture_fields import (
    LOG_CAPTURE_FIELDS,
)
from tests.unit.nodes.node_dev_roll_failure_capture_effect.conftest import (
    NOW,
    POLICY,
    incident_payload,
)

pytestmark = pytest.mark.unit

INCIDENT_FIELDS = {
    "event_id",
    "job_id",
    "issuer",
    "commit",
    "error",
    "project",
    "at",
    "restored_revision",
    "recovery_verified",
    "recovery_skipped_healthy",
    "recovery_error",
    "runtime_kept_revision",
    "health_probes",
    "log_captures",
}
MANIFEST_FIELDS = {
    "event_id",
    "job_id",
    "issuer",
    "git_ref",
    "scope",
    "requested_at",
    "accepted_at",
    "open_pr",
    "rollback",
}
LOG_CAPTURE_FIELDS_CONSTANT = {
    "service",
    "container",
    "bytes_total",
    "bytes_kept",
    "complete",
    "redacted",
    "sha256",
    "artifact_path",
    "error",
    "log",
}

ROOT = Path(__file__).resolve().parents[4]
CONTRACT = (
    ROOT / "src/omnimarket/nodes/node_dev_roll_failure_capture_effect/contract.yaml"
)
_CONTRACT = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
_SUBSCRIBE_TOPICS = _CONTRACT["event_bus"]["subscribe_topics"]


def test_our_incident_event_has_exactly_the_producers_fields() -> None:
    assert set(ModelDevRollIncidentEvent.model_fields) == INCIDENT_FIELDS, (
        "ModelDevRollIncidentEvent (ours) or the deploy agent's incident event (theirs) changed shape"
    )


def test_our_manifest_event_has_exactly_the_producers_fields() -> None:
    assert set(ModelDevRollManifestEvent.model_fields) == MANIFEST_FIELDS, (
        "ModelDevRollManifestEvent (ours) or the deploy agent's manifest event (theirs) changed shape"
    )


def test_our_log_capture_fields_are_the_spelled_set() -> None:
    assert set(LOG_CAPTURE_FIELDS) == LOG_CAPTURE_FIELDS_CONSTANT, (
        "LOG_CAPTURE_FIELDS (ours) or the deploy agent's log capture fields (theirs) changed shape"
    )


def test_our_contract_declares_the_two_topics() -> None:
    assert "onex.evt.omnimarket.dev-roll-manifest.v1" in _SUBSCRIBE_TOPICS, (
        "contract.yaml (ours) renamed the manifest topic"
    )
    assert "onex.evt.omnimarket.dev-roll-incident.v1" in _SUBSCRIBE_TOPICS, (
        "contract.yaml (ours) renamed the incident topic"
    )


def test_our_contract_publishes_the_recorded_and_terminal_topics() -> None:
    published = {
        entry["event_type"]: entry["topic"] for entry in _CONTRACT["published_events"]
    }
    assert published == {
        "DevRollManifestRecorded": "onex.evt.omnimarket.dev-roll-manifest-recorded.v1",
        "DevRollTerminal": "onex.evt.omnimarket.dev-roll-terminal.v1",
    }, "contract.yaml (ours) renamed a topic the roll terminal is read from"


def test_a_payload_from_the_spelled_sets_is_read_as_recovered() -> None:
    store = InMemoryDevRollArtifactStore()
    handler = HandlerDevRollIncidentCapture(store, policy=POLICY, clock=lambda: NOW)
    payload = incident_payload()
    terminal = handler.handle(ModelDevRollIncidentEvent.model_validate(payload))
    assert isinstance(terminal, ModelDevRollTerminal)
    assert terminal.outcome is EnumDevRollOutcome.RECOVERED
