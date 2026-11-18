# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Join: the request effect and node_dev_roll_request_compute on one in-memory bus, over both real contracts.

A manifest becomes facts on the request topic, the batching node decides, and the decided event
comes back to the effect. Shadow only: no deploy-agent command appears on the bus.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from omnimarket.models.dev_roll import (
    EnumDevRollAction,
    EnumDevRollEventRefusal,
    EnumDevRollHoldReason,
    ModelDevRollDecided,
    ModelDevRollFacts,
)
from omnimarket.nodes.node_dev_roll_decision_request_effect.handlers import (
    HandlerDevRollQueuedRoll,
)
from omnimarket.nodes.node_dev_roll_decision_request_effect.handlers.store_dev_roll_observation import (
    InMemoryDevRollObservationStore,
)
from omnimarket.nodes.node_dev_roll_decision_request_effect.models import (
    ModelDevRollDecisionRequestSkipped,
    ModelDevRollManifestEvent,
    ModelDevRollObservationRecorded,
)
from omnimarket.nodes.node_dev_roll_request_compute.handlers import (
    HandlerDevRollRequest,
)
from omnimarket.nodes.node_dev_roll_request_compute.overlay import load_dev_roll_policy
from tests.unit.nodes.node_dev_roll_decision_request_effect.chain_support import (
    DECIDED_TOPIC,
    FACTS_TOPIC,
    KEY,
    MANIFEST_TOPIC,
    RECORDED_TOPIC,
    SKIPPED_TOPIC,
    envelope,
    join_chain,
    on,
)
from tests.unit.nodes.node_dev_roll_decision_request_effect.conftest import (
    NOW,
    POLICY,
    manifest_payload,
)
from tests.unit.nodes.node_dev_roll_request_compute.conftest import write_overlay

pytestmark = pytest.mark.unit

WINDOW = timedelta(seconds=1800)


@pytest.fixture
def compute(tmp_path: Path) -> HandlerDevRollRequest:
    return HandlerDevRollRequest(load_dev_roll_policy(write_overlay(tmp_path)))


async def _run(dispatch, transport, payload) -> None:
    await transport.send(MANIFEST_TOPIC, key=KEY, value=envelope(payload), headers={})
    while await dispatch.drain():
        pass


def _decisions(broker) -> list[ModelDevRollDecided]:
    return [
        ModelDevRollDecided.model_validate(m.payload) for m in on(broker, DECIDED_TOPIC)
    ]


async def test_five_manifests_in_one_window_are_five_holds_and_no_roll(
    store: InMemoryDevRollObservationStore, compute: HandlerDevRollRequest
) -> None:
    dispatch, transport, broker, _ = join_chain(store, compute, NOW)
    for age in (1500, 1200, 900, 600, 300):
        await _run(dispatch, transport, manifest_payload(age_seconds=age))
    assert len(on(broker, FACTS_TOPIC)) == 5
    decisions = _decisions(broker)
    assert len(decisions) == 5
    assert all(d.action is EnumDevRollAction.HOLD for d in decisions)
    assert all(d.hold_reason is EnumDevRollHoldReason.WINDOW_OPEN for d in decisions)
    assert [len(f.payload["pending_events"]) for f in on(broker, FACTS_TOPIC)] == [
        1,
        2,
        3,
        4,
        5,
    ]


async def test_after_the_window_one_roll_covers_every_pending_event_and_the_next_waits(
    store: InMemoryDevRollObservationStore, compute: HandlerDevRollRequest
) -> None:
    dispatch, transport, _broker, _ = join_chain(store, compute, NOW)
    for age in (1500, 1200, 900, 600, 300):
        await _run(dispatch, transport, manifest_payload(age_seconds=age))
    late = NOW + WINDOW + timedelta(seconds=60)
    dispatch2, transport2, broker2, _ = join_chain(store, compute, late)
    newest = "b" * 40
    await _run(dispatch2, transport2, manifest_payload(age_seconds=0, sha=newest))
    (decided,) = _decisions(broker2)
    assert decided.action is EnumDevRollAction.ROLL_NOW
    assert decided.would_request is not None
    assert len(decided.rolled_event_ids) == 6
    (recorded,) = (
        ModelDevRollObservationRecorded.model_validate(m.payload)
        for m in on(broker2, RECORDED_TOPIC)
    )
    assert recorded.cleared_count == 6
    assert store.load().pending_events == ()
    # The shadow roll is the last roll now: the next manifest is held by the minimum start interval.
    later = late + timedelta(seconds=POLICY.shadow_roll_duration_seconds + 1)
    dispatch3, transport3, broker3, _ = join_chain(store, compute, later)
    await _run(dispatch3, transport3, manifest_payload(age_seconds=0))
    (next_decision,) = _decisions(broker3)
    assert next_decision.action is EnumDevRollAction.HOLD
    assert next_decision.hold_reason is EnumDevRollHoldReason.MIN_START_INTERVAL


async def test_one_manifest_yields_exactly_one_decision(
    store: InMemoryDevRollObservationStore, compute: HandlerDevRollRequest
) -> None:
    dispatch, transport, broker, _ = join_chain(store, compute, NOW)
    await _run(dispatch, transport, manifest_payload(age_seconds=60))
    assert len(on(broker, FACTS_TOPIC)) == 1
    assert len(_decisions(broker)) == 1
    assert on(broker, SKIPPED_TOPIC) == []


async def test_an_unknown_repository_is_refused_on_the_decision_not_dropped(
    store: InMemoryDevRollObservationStore, compute: HandlerDevRollRequest
) -> None:
    dispatch, transport, broker, _ = join_chain(store, compute, NOW)
    await _run(
        dispatch,
        transport,
        manifest_payload(age_seconds=60, issuer="gha/some_other_repo/pr-3"),
    )
    (decided,) = _decisions(broker)
    (refused,) = decided.refused_events
    assert refused.reason is EnumDevRollEventRefusal.UNKNOWN_REPOSITORY


async def test_shadow_only_no_deploy_agent_command_is_on_the_bus(
    store: InMemoryDevRollObservationStore, compute: HandlerDevRollRequest
) -> None:
    dispatch, transport, broker, _ = join_chain(store, compute, NOW + WINDOW * 2)
    await _run(dispatch, transport, manifest_payload(age_seconds=3600))
    (decided,) = _decisions(broker)
    assert decided.action is EnumDevRollAction.ROLL_NOW
    published = {
        topic
        for topic in (FACTS_TOPIC, DECIDED_TOPIC, RECORDED_TOPIC, SKIPPED_TOPIC)
        if on(broker, topic)
    }
    assert published == {FACTS_TOPIC, DECIDED_TOPIC, RECORDED_TOPIC}
    assert not any(t.startswith("onex.cmd.deploy") for t in published)


class _Silent(HandlerDevRollQueuedRoll):
    def handle(
        self, request: ModelDevRollManifestEvent
    ) -> ModelDevRollFacts | ModelDevRollDecisionRequestSkipped:
        raise RuntimeError("publishes nothing")


async def test_failing_control_a_silent_queued_handler_yields_no_decision(
    store: InMemoryDevRollObservationStore, compute: HandlerDevRollRequest
) -> None:
    # swap in the stand-in: the join built its own handler, so rebuild the chain with a silent one
    from tests.unit.nodes.node_dev_roll_decision_request_effect import chain_support

    silent = _Silent(store, policy=POLICY, clock=lambda: NOW)
    dispatch, transport, broker, _ = chain_support._build(
        [chain_support.CONTRACT, chain_support.COMPUTE_CONTRACT],
        {
            "HandlerDevRollQueuedRoll": silent,
            "HandlerDevRollDecidedObserved": chain_support.HandlerDevRollDecidedObserved(
                store, policy=POLICY
            ),
            "HandlerDevRollRequest": compute,
        },
        NOW,
    )
    await _run(dispatch, transport, manifest_payload(age_seconds=60))
    assert len(_decisions(broker)) != 1, (
        "one manifest -> one decision must fail for a silent request effect"
    )


def test_the_facts_model_is_the_shared_dev_roll_model() -> None:
    assert ModelDevRollFacts.__module__.startswith("omnimarket.models.dev_roll")
