# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seams of node_dev_roll_window_gate_effect: each pins one side against the other, so a change of shape on either goes red.

Seams: (1) the deploy command the effect forwards and the gate holds (ModelDeployPublishCommand), (2) the
hold topic between node_redeploy_deploy_effect and the gate, (3) the deploy-publish topic back from the gate
to the effect and the orchestrator's own spelling of it, (4) the agent's completion event the gate reads,
(5) the batching node's facts, tip event, record and policy the gate builds, (6) the overlay document and
its shipped example.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import BaseModel, create_model

from omnimarket.events.runtime_deployment import (
    ModelDeployPublishCommand,
    ModelDeployRebuildCompleted,
)
from omnimarket.models.dev_roll import (
    ModelDevRollDecided,
    ModelDevRollFacts,
    ModelDevRollRecord,
    ModelDevTipChanged,
)
from omnimarket.nodes.node_dev_roll_request_compute.handlers import decide_dev_roll
from omnimarket.nodes.node_dev_roll_request_compute.models import ModelDevRollPolicy
from omnimarket.nodes.node_dev_roll_window_gate_effect.models import (
    ModelDevRollWindowGatePolicy,
)
from omnimarket.nodes.node_redeploy_deploy_effect.handlers.handler_deploy_publish_monitor import (
    TOPIC_DEV_ROLL_HOLD,
    TOPIC_REBUILD_COMPLETED,
)
from omnimarket.nodes.node_redeploy_orchestrator.handlers.handler_redeploy_orchestrator import (
    TOPIC_DEPLOY_PUBLISH,
)
from tests.unit.nodes.node_dev_roll_window_gate_effect.chain_support import (
    COMPLETED_TOPIC,
    DEPLOY_PUBLISH_TOPIC,
    HOLD_TOPIC,
)
from tests.unit.nodes.node_dev_roll_window_gate_effect.conftest import (
    NOW,
    POLICY,
    WINDOW,
    command,
    sha,
)

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[4]
COMMAND_FIELDS_READ = {
    "correlation_id",
    "requested_by",
    "requested_at",
    "git_ref",
    "runtime_lane",
    "image_ref",
    "image_digest",
    "roll_window_released",
}
COMPLETED_FIELDS_READ = {"correlation_id", "completed_at", "status"}
FACTS_FIELDS = {"correlation_id", "last_roll", "observed_at", "pending_events"}
TIP_FIELDS = {"event_id", "merged_at", "repository", "sha"}
RECORD_FIELDS = {
    "finished_at",
    "git_ref",
    "health",
    "healthy_since",
    "job_id",
    "runtime_started_at",
}


def missing_fields(model: type[BaseModel], needed: set[str]) -> set[str]:
    return needed - set(model.model_fields)


def test_the_deploy_command_still_carries_every_field_the_gate_reads() -> None:
    assert missing_fields(ModelDeployPublishCommand, COMMAND_FIELDS_READ) == set()


def test_the_rebuild_completed_event_still_carries_every_field_the_gate_reads() -> None:
    assert missing_fields(ModelDeployRebuildCompleted, COMPLETED_FIELDS_READ) == set()


def test_the_batching_nodes_models_are_pinned_to_what_the_gate_builds() -> None:
    assert set(ModelDevRollFacts.model_fields) == FACTS_FIELDS
    assert set(ModelDevTipChanged.model_fields) == TIP_FIELDS
    assert set(ModelDevRollRecord.model_fields) == RECORD_FIELDS


def test_the_gates_policy_supplies_every_field_of_the_batching_policy() -> None:
    built = POLICY.to_roll_policy()
    assert set(type(built).model_fields) == set(ModelDevRollPolicy.model_fields)
    assert built.model_dump().keys() == ModelDevRollPolicy.model_fields.keys()


def test_a_live_policy_makes_the_decision_a_real_one_not_a_shadow() -> None:
    held = [
        ModelDevTipChanged(
            event_id=command(number=1).correlation_id,
            repository="omnimarket",
            sha=sha(1),
            merged_at=NOW - timedelta(seconds=WINDOW),
        )
    ]
    facts = ModelDevRollFacts(
        correlation_id=command(number=1).correlation_id,
        observed_at=NOW,
        pending_events=tuple(held),
        last_roll=None,
    )
    live = decide_dev_roll(facts, POLICY.to_roll_policy())
    shadow = decide_dev_roll(
        facts, POLICY.to_roll_policy().model_copy(update={"mode": "shadow"})
    )
    assert isinstance(live, ModelDevRollDecided)
    assert isinstance(shadow, ModelDevRollDecided)
    assert live.shadow is False
    assert shadow.shadow is True


def test_the_hold_topic_is_one_topic_in_both_contracts() -> None:
    assert TOPIC_DEV_ROLL_HOLD == HOLD_TOPIC


def test_the_deploy_publish_topic_is_one_topic_in_the_gate_the_effect_and_the_orchestrator() -> (
    None
):
    assert DEPLOY_PUBLISH_TOPIC == TOPIC_DEPLOY_PUBLISH
    effect = yaml.safe_load(
        (
            ROOT / "src/omnimarket/nodes/node_redeploy_deploy_effect/contract.yaml"
        ).read_text(encoding="utf-8")
    )
    assert DEPLOY_PUBLISH_TOPIC in effect["event_bus"]["subscribe_topics"]


def test_the_completion_topic_is_the_deploy_effects_own() -> None:
    assert COMPLETED_TOPIC == TOPIC_REBUILD_COMPLETED


def test_the_overlay_example_carries_exactly_the_policys_fields() -> None:
    doc = yaml.safe_load(
        (
            ROOT
            / "src/omnimarket/nodes/node_dev_roll_window_gate_effect/overlay.example.yaml"
        ).read_text(encoding="utf-8")
    )
    assert set(doc) == set(ModelDevRollWindowGatePolicy.model_fields)


@pytest.mark.parametrize("field", sorted(COMMAND_FIELDS_READ))
def test_failing_control_a_deploy_command_renamed_field_is_caught(field: str) -> None:
    fields: dict[str, Any] = {
        name: (info.annotation, info)
        for name, info in ModelDeployPublishCommand.model_fields.items()
        if name != field
    }
    renamed = create_model("Renamed", **fields)
    assert missing_fields(renamed, COMMAND_FIELDS_READ) == {field}


@pytest.mark.parametrize("field", sorted(COMPLETED_FIELDS_READ))
def test_failing_control_a_completed_event_renamed_field_is_caught(field: str) -> None:
    fields: dict[str, Any] = {
        name: (info.annotation, info)
        for name, info in ModelDeployRebuildCompleted.model_fields.items()
        if name != field
    }
    renamed = create_model("Renamed", **fields)
    assert missing_fields(renamed, COMPLETED_FIELDS_READ) == {field}


def test_failing_control_a_facts_field_rename_leaves_the_pinned_set() -> None:
    renamed = (set(ModelDevRollFacts.model_fields) - {"pending_events"}) | {"pending"}
    assert renamed != FACTS_FIELDS
