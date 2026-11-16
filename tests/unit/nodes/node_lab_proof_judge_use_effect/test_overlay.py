# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Overlay: example loads; each required key missing refuses; empty topic refused."""

from __future__ import annotations

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_lab_proof_judge_use_effect.overlay import (
    ModelLabProofJudgeUseOverlay,
)
from tests.unit.nodes.node_lab_proof_judge_use_effect.conftest import (
    _EXAMPLE,
    USE_TOPIC,
)

VALID = {
    "schema_version": 1,
    "event": {
        "topic": USE_TOPIC,
        "event_type": "lab_proof_judge.use_recorded",
    },
    "ledger": {
        "append": {
            "command_env": "EXAMPLE_LEDGER_APPEND_COMMAND",
            "host_env": "EXAMPLE_HOST_NAME",
            "timeout_seconds": 60,
        },
        "lane": "lab-proof-judge",
        "actor": "node:node_lab_proof_judge_use_effect",
        "ticket": "OMN-23642",
        "kind_cell": "lab-proof-class-use",
    },
}


def test_example_overlay_loads() -> None:
    overlay = ModelLabProofJudgeUseOverlay(**VALID)
    assert overlay.schema_version == 1
    assert overlay.event.topic == USE_TOPIC
    assert overlay.ledger.lane == "lab-proof-judge"
    assert overlay.ledger.ticket == "OMN-23642"


def test_missing_schema_version_refused() -> None:
    data = {k: v for k, v in VALID.items() if k != "schema_version"}
    with pytest.raises(ValidationError):
        ModelLabProofJudgeUseOverlay(**data)


def test_missing_event_refused() -> None:
    data = {k: v for k, v in VALID.items() if k != "event"}
    with pytest.raises(ValidationError):
        ModelLabProofJudgeUseOverlay(**data)


def test_missing_ledger_refused() -> None:
    data = {k: v for k, v in VALID.items() if k != "ledger"}
    with pytest.raises(ValidationError):
        ModelLabProofJudgeUseOverlay(**data)


def test_empty_topic_refused() -> None:
    data = dict(VALID)
    data["event"] = {"topic": "", "event_type": "lab_proof_judge.use_recorded"}
    with pytest.raises(ValidationError):
        ModelLabProofJudgeUseOverlay(**data)


def test_empty_lane_refused() -> None:
    data = dict(VALID)
    ledger = dict(data["ledger"])
    ledger["lane"] = ""
    data["ledger"] = ledger
    with pytest.raises(ValidationError):
        ModelLabProofJudgeUseOverlay(**data)


def test_empty_ticket_refused() -> None:
    data = dict(VALID)
    ledger = dict(data["ledger"])
    ledger["ticket"] = ""
    data["ledger"] = ledger
    with pytest.raises(ValidationError):
        ModelLabProofJudgeUseOverlay(**data)


def test_declared_event_is_registered_with_the_emit_registry() -> None:
    """The seam to the emit effect: the overlay's topic and event type resolve a posture, so the event is deliverable (not refused)."""
    from omnimarket.nodes.node_event_emit_effect.spool.topic_resolver import (
        resolve_override_transform,
    )

    example = yaml.safe_load(_EXAMPLE.read_text())
    resolve_override_transform(
        example["event"]["topic"], example["event"]["event_type"]
    )
