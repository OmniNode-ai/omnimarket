# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam: the decision request this node publishes, against the batching node that reads it, and the manifest it reads.

Two sides are pinned. The deploy agent's manifest model lives in omnibase_infra, which this
test imports (the same way the failure capture node's seam test imports the agent's incident
model), and the spelled field set is held here too. The compute node's input model, event and
record models are pinned by their field sets, so a rename on either side goes red here.
"""

from __future__ import annotations

import pytest
import yaml
from omnibase_infra.models.model_dev_roll_manifest import ModelDevRollManifest

from omnimarket.events.dev_roll import (
    ModelDevRollDecided,
    ModelDevRollFacts,
    ModelDevRollRecord,
    ModelDevTipChanged,
)
from omnimarket.nodes.node_dev_roll_decision_request_effect.handlers import (
    HandlerDevRollQueuedRoll,
)
from omnimarket.nodes.node_dev_roll_decision_request_effect.handlers.store_dev_roll_observation import (
    InMemoryDevRollObservationStore,
)
from omnimarket.nodes.node_dev_roll_decision_request_effect.models import (
    ModelDevRollManifestEvent,
)
from omnimarket.nodes.node_dev_roll_request_compute.handlers import decide_dev_roll
from omnimarket.nodes.node_dev_roll_request_compute.models import (
    ModelDevRollPolicy,
)
from tests.unit.nodes.node_dev_roll_decision_request_effect.chain_support import (
    COMPUTE_CONTRACT,
    DECIDED_TOPIC,
    FACTS_TOPIC,
)
from tests.unit.nodes.node_dev_roll_decision_request_effect.conftest import (
    NOW,
    POLICY,
    SHA,
    manifest_payload,
)
from tests.unit.nodes.node_dev_roll_request_compute.conftest import POLICY_DOC

pytestmark = pytest.mark.unit

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
FACTS_FIELDS = ("correlation_id", "last_roll", "observed_at", "pending_events")
TIP_FIELDS = ("event_id", "merged_at", "repository", "sha")
RECORD_FIELDS = (
    "finished_at",
    "git_ref",
    "health",
    "healthy_since",
    "job_id",
    "runtime_started_at",
)
_COMPUTE = yaml.safe_load(COMPUTE_CONTRACT.read_text(encoding="utf-8"))


def test_our_manifest_event_has_exactly_the_deploy_agents_fields() -> None:
    assert set(ModelDevRollManifestEvent.model_fields) == MANIFEST_FIELDS, (
        "ModelDevRollManifestEvent (ours) or the spelled manifest fields changed shape"
    )
    assert set(ModelDevRollManifestEvent.model_fields) == set(
        ModelDevRollManifest.model_fields
    ), (
        "ModelDevRollManifestEvent (ours) and omnibase_infra ModelDevRollManifest (theirs) differ"
    )


def test_the_facts_event_record_fields_are_pinned() -> None:
    assert tuple(sorted(ModelDevRollFacts.model_fields)) == FACTS_FIELDS
    assert tuple(sorted(ModelDevTipChanged.model_fields)) == TIP_FIELDS
    assert tuple(sorted(ModelDevRollRecord.model_fields)) == RECORD_FIELDS


def test_the_request_topic_is_the_compute_nodes_command_topic() -> None:
    assert _COMPUTE["runtime_dispatch"]["command_topic"] == FACTS_TOPIC
    assert FACTS_TOPIC in _COMPUTE["event_bus"]["subscribe_topics"]
    assert _COMPUTE["runtime_dispatch"]["terminal_events"]["success"] == DECIDED_TOPIC


def test_the_facts_event_type_is_the_compute_nodes_input_model_name() -> None:
    assert _COMPUTE["input_model"]["name"] == "ModelDevRollFacts"
    assert ModelDevRollFacts.__name__.removeprefix("Model") == "DevRollFacts"


def _facts() -> ModelDevRollFacts:
    handler = HandlerDevRollQueuedRoll(
        InMemoryDevRollObservationStore(), policy=POLICY, clock=lambda: NOW
    )
    facts = handler.handle(
        ModelDevRollManifestEvent.model_validate(manifest_payload(age_seconds=60))
    )
    assert isinstance(facts, ModelDevRollFacts)
    return facts


def test_a_published_facts_message_is_accepted_by_the_batching_decision() -> None:
    facts = _facts()
    assert facts.pending_events[0].sha == SHA
    decided = decide_dev_roll(facts, ModelDevRollPolicy.model_validate(POLICY_DOC))
    assert isinstance(decided, ModelDevRollDecided)
    assert decided.shadow is True


def test_failing_control_a_renamed_facts_field_is_rejected() -> None:
    body = _facts().model_dump(mode="json")
    body["pending"] = body.pop("pending_events")
    with pytest.raises(ValueError, match="pending_events"):
        ModelDevRollFacts.model_validate(body)
    renamed = (set(ModelDevRollFacts.model_fields) - {"pending_events"}) | {"pending"}
    assert tuple(sorted(renamed)) != FACTS_FIELDS, (
        "a renamed field must not equal the pinned set"
    )


def test_failing_control_a_renamed_manifest_field_leaves_the_agents_set() -> None:
    renamed = (set(ModelDevRollManifestEvent.model_fields) - {"git_ref"}) | {"ref"}
    assert renamed != set(ModelDevRollManifest.model_fields)
