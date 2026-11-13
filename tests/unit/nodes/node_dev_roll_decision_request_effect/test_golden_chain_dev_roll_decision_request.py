# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain for node_dev_roll_decision_request_effect over its real contract on the in-memory bus.

One manifest in, one decision request out on the compute node's command topic, or a
typed skip; one roll_now decision of the compute node clears the pending events it
rolled and becomes the shadow last roll, a hold changes nothing. Topic bytes in, typed
models out, through the runtime's own path: discovery reads ``contract.yaml``,
``build_routing_map`` resolves each subscribed topic to its handler and its
``event_model``, ``RuntimeDispatch`` decodes the envelope and coerces the payload, and
the handler's return goes out on the topic ``published_events`` names.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from omnimarket.events.dev_roll import (
    EnumDevRollAction,
    EnumDevRollHealth,
    ModelDevRollDecided,
    ModelDevRollFacts,
)
from omnimarket.nodes.node_dev_roll_decision_request_effect.handlers import (
    HandlerDevRollDecidedObserved,
    HandlerDevRollQueuedRoll,
)
from omnimarket.nodes.node_dev_roll_decision_request_effect.models import (
    ModelDevRollObservationRecorded,
)
from omnimarket.nodes.node_dev_roll_request_compute.handlers import decide_dev_roll
from omnimarket.nodes.node_dev_roll_request_compute.models import (
    ModelDevRollPolicy,
)
from omnimarket.nodes.node_dev_roll_request_compute.overlay import load_dev_roll_policy
from tests.unit.nodes.node_dev_roll_decision_request_effect.chain_support import (
    DECIDED_TOPIC,
    FACTS_TOPIC,
    KEY,
    MANIFEST_TOPIC,
    RECORDED_TOPIC,
    SKIPPED_TOPIC,
    chain,
    chain_with,
    envelope,
    on,
)
from tests.unit.nodes.node_dev_roll_decision_request_effect.conftest import (
    NOW,
    POLICY,
    SHA,
    SHADOW_ROLL_SECONDS,
    manifest_payload,
)
from tests.unit.nodes.node_dev_roll_request_compute.conftest import (
    POLICY_DOC as COMPUTE_POLICY_DOC,
)
from tests.unit.nodes.node_dev_roll_request_compute.conftest import (
    facts as compute_facts,
)
from tests.unit.nodes.node_dev_roll_request_compute.conftest import (
    tip as compute_tip,
)
from tests.unit.nodes.node_dev_roll_request_compute.conftest import (
    write_overlay as write_compute_overlay,
)

pytestmark = pytest.mark.unit

# The batching window the compute policy is read with: the golden manifests are merged
# _EVENT_AGE_SECONDS before NOW, so the window is closed and the decide rolls now.
_WINDOW_SECONDS = 30
_EVENT_AGE_SECONDS = 60


@pytest.fixture
def compute_policy(tmp_path: Path) -> ModelDevRollPolicy:
    """The compute node's batching policy, read through its overlay.

    The window is small enough that the golden manifest's event is older than it, and
    the build context is the repository the manifests' issuer names, so the rolled
    decision carries the manifest's own sha as its git_ref.
    """
    doc = {
        **COMPUTE_POLICY_DOC,
        "batch_window_seconds": _WINDOW_SECONDS,
        "known_repositories": ["omnimarket"],
        "build_context_repository": "omnimarket",
    }
    return load_dev_roll_policy(write_compute_overlay(tmp_path, doc))


async def _send_manifest(transport, payload: dict[str, object]) -> None:
    await transport.send(
        MANIFEST_TOPIC,
        key=KEY,
        value=envelope(payload),
        headers={},
    )


async def _send_decided(transport, decided: ModelDevRollDecided) -> None:
    await transport.send(
        DECIDED_TOPIC,
        key=KEY,
        value=envelope(
            decided.model_dump(mode="json"),
            decided.correlation_id,
            payload_type="ModelDevRollDecided",
        ),
        headers={},
    )


def _facts(broker) -> list[ModelDevRollFacts]:
    return [
        ModelDevRollFacts.model_validate(message.payload)
        for message in on(broker, FACTS_TOPIC)
    ]


def _recorded(broker) -> ModelDevRollObservationRecorded:
    (message,) = on(broker, RECORDED_TOPIC)
    return ModelDevRollObservationRecorded.model_validate(message.payload)


def _roll_now(
    facts_payload: dict[str, object], policy: ModelDevRollPolicy
) -> ModelDevRollDecided:
    """The compute node's decision on the facts one facts message carried."""
    decided = decide_dev_roll(ModelDevRollFacts.model_validate(facts_payload), policy)
    assert isinstance(decided, ModelDevRollDecided)
    assert decided.action is EnumDevRollAction.ROLL_NOW
    return decided


def test_the_subscribed_topics_are_the_contracts_two(store) -> None:
    _dispatch, _transport, _broker, topics = chain(store)
    assert topics == {MANIFEST_TOPIC, DECIDED_TOPIC}


async def test_golden_one_manifest_yields_one_decision_request(store) -> None:
    dispatch, transport, broker, _topics = chain(store)
    await _send_manifest(transport, manifest_payload(job_id="job-1"))
    assert await dispatch.drain() == 1
    (facts,) = _facts(broker)
    assert len(facts.pending_events) == 1
    (event,) = facts.pending_events
    assert event.repository == "omnimarket"
    assert event.sha == SHA
    assert event.merged_at == NOW
    assert facts.last_roll is None
    assert facts.observed_at == NOW
    assert on(broker, SKIPPED_TOPIC) == []


async def test_golden_three_manifests_batch_into_three_pending_events(store) -> None:
    dispatch, transport, broker, _topics = chain(store)
    for n in range(1, 4):
        await _send_manifest(transport, manifest_payload(job_id=f"job-{n}"))
    assert await dispatch.drain() == 3
    facts = _facts(broker)
    assert len(facts) == 3
    assert [len(entry.pending_events) for entry in facts] == [1, 2, 3]
    for entry in facts:
        assert entry.pending_events[-1].repository == "omnimarket"
        assert entry.pending_events[-1].sha == SHA


async def test_golden_a_roll_now_clears_its_events_and_sets_the_shadow_roll(
    store, compute_policy: ModelDevRollPolicy
) -> None:
    dispatch, transport, broker, _topics = chain(store)
    for n in range(1, 4):
        await _send_manifest(
            transport,
            manifest_payload(job_id=f"job-{n}", age_seconds=_EVENT_AGE_SECONDS),
        )
    assert await dispatch.drain() == 3
    decided = _roll_now(on(broker, FACTS_TOPIC)[-1].payload, compute_policy)
    assert len(decided.rolled_event_ids) == 3
    await _send_decided(transport, decided)
    assert await dispatch.drain() == 1
    recorded = _recorded(broker)
    assert recorded.correlation_id == decided.correlation_id
    assert recorded.action == "roll_now"
    assert recorded.cleared_count == len(decided.rolled_event_ids)
    assert recorded.recorded is True
    assert recorded.error is None

    # The next manifest inside the shadow roll duration carries only its own pending
    # event, and the last roll reads unknown at the decided decision's git_ref.
    inside, transport_inside, broker_inside, _inside_topics = chain(store, clock=NOW)
    await _send_manifest(transport_inside, manifest_payload(job_id="job-4"))
    assert await inside.drain() == 1
    (facts,) = _facts(broker_inside)
    assert len(facts.pending_events) == 1
    assert facts.pending_events[0].repository == "omnimarket"
    assert facts.last_roll is not None
    assert facts.last_roll.git_ref == decided.would_request.git_ref
    assert facts.last_roll.git_ref == SHA
    assert facts.last_roll.job_id == str(decided.correlation_id)
    assert facts.last_roll.health is EnumDevRollHealth.UNKNOWN
    assert facts.last_roll.finished_at is None

    # Past the shadow roll duration the same last roll reads healthy.
    finished, transport_finished, broker_finished, _finished_topics = chain(
        store, clock=NOW + timedelta(seconds=SHADOW_ROLL_SECONDS + 1)
    )
    await _send_manifest(transport_finished, manifest_payload(job_id="job-5"))
    assert await finished.drain() == 1
    (facts,) = _facts(broker_finished)
    assert facts.last_roll is not None
    assert facts.last_roll.git_ref == SHA
    assert facts.last_roll.health is EnumDevRollHealth.HEALTHY
    assert facts.last_roll.finished_at == NOW + timedelta(seconds=SHADOW_ROLL_SECONDS)
    assert facts.last_roll.healthy_since == facts.last_roll.finished_at


async def test_golden_a_hold_decision_changes_nothing(
    store, compute_policy: ModelDevRollPolicy
) -> None:
    dispatch, transport, broker, _topics = chain(store)
    await _send_manifest(transport, manifest_payload(job_id="job-1"))
    assert await dispatch.drain() == 1
    assert len(_facts(broker)) == 1
    before = store.load()
    young = compute_facts(
        (compute_tip("omnimarket", age_seconds=_WINDOW_SECONDS - 10),)
    )
    hold = decide_dev_roll(young, compute_policy)
    assert isinstance(hold, ModelDevRollDecided)
    assert hold.action is EnumDevRollAction.HOLD
    await _send_decided(transport, hold)
    assert await dispatch.drain() == 1
    recorded = _recorded(broker)
    assert recorded.correlation_id == hold.correlation_id
    assert recorded.action == "hold"
    assert recorded.cleared_count == 0
    assert recorded.recorded is True
    assert recorded.error is None
    assert store.load() == before
    assert len(_facts(broker)) == 1


class _PublishesNothing(HandlerDevRollQueuedRoll):
    """Failing control: a queued-roll handler that publishes nothing."""

    def handle(self, request) -> None:
        return None


async def test_failing_control_a_queued_roll_that_publishes_nothing_yields_no_facts(
    store,
) -> None:
    """The golden expectation (one facts message) is red against this stand-in."""
    dispatch, transport, broker, _topics = chain_with(
        _PublishesNothing(store, policy=POLICY, clock=lambda: NOW),
        HandlerDevRollDecidedObserved(store, policy=POLICY),
    )
    await _send_manifest(transport, manifest_payload(job_id="job-1"))
    assert await dispatch.drain() == 1
    assert _facts(broker) == []
    assert on(broker, SKIPPED_TOPIC) == []
