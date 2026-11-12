# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam tests with the deploy agent (omnibase_infra, OMN-24013).

The other repository cannot be imported here and a skip is refused by the skip
ratchet, so both shapes are spelled. When the other side changes, this spelled
contract is updated in the change that follows it.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from omnimarket.nodes.node_dev_roll_failure_capture_effect.models import (
    ModelDevLogCapture,
    ModelDevRollIncident,
    ModelDevRollManifest,
)

pytestmark = pytest.mark.unit

# Agent wire fields (omnibase_infra, scripts/deploy-agent/deploy_agent/events.py,
# ticket OMN-24013, branch omn-24013-deploy-start-incident-events)
AGENT_INCIDENT_FIELDS: set[str] = {
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

AGENT_LOG_CAPTURE_FIELDS: set[str] = {
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

AGENT_MANIFEST_FIELDS: set[str] = {
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

AGENT_TOPICS: set[str] = {
    "onex.evt.omnimarket.dev-roll-incident.v1",
    "onex.evt.omnimarket.dev-roll-manifest.v1",
}


class TestSeamAgentEvents:
    def test_incident_model_fields_match_agent(self) -> None:
        assert set(ModelDevRollIncident.model_fields.keys()) == AGENT_INCIDENT_FIELDS

    def test_log_capture_model_fields_match_agent(self) -> None:
        assert set(ModelDevLogCapture.model_fields.keys()) == AGENT_LOG_CAPTURE_FIELDS

    def test_manifest_model_fields_match_agent(self) -> None:
        assert set(ModelDevRollManifest.model_fields.keys()) == AGENT_MANIFEST_FIELDS

    def test_contract_subscribes_exactly_two_topics(self) -> None:
        import yaml
        from pathlib import Path

        contract_path = Path(
            "src/omnimarket/nodes/node_dev_roll_failure_capture_effect/contract.yaml"
        )
        with open(contract_path) as f:
            contract = yaml.safe_load(f)
        subscribe_topics = set(contract["event_bus"]["subscribe_topics"])
        assert subscribe_topics == AGENT_TOPICS

    def test_agent_json_payload_validates_in_model(self) -> None:
        """A payload built by dumping a dict shaped like the agent's JSON validates in ModelDevRollIncident."""
        agent_payload = {
            "event_id": str(uuid.uuid4()),
            "job_id": "job-456",
            "issuer": "deploy-agent",
            "commit": "abc123",
            "error": "crash",
            "project": "proj",
            "at": datetime.now(timezone.utc).isoformat(),
            "restored_revision": "def456",
            "recovery_verified": True,
            "recovery_skipped_healthy": None,
            "recovery_error": None,
            "runtime_kept_revision": None,
            "health_probes": [],
            "log_captures": [],
        }
        incident = ModelDevRollIncident(**agent_payload)
        assert incident.job_id == "job-456"
