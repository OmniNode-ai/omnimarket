# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain for node_dev_roll_decision_request_effect: the same runtime path, a failing input each.

One manifest the handler cannot turn into a decision request in, one typed
``ModelDevRollDecisionRequestSkipped`` out and nothing on the decision-request topic;
one decision whose observation state cannot be stored in, one ``recorded=False``
observation record out with the error text.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_dev_roll_decision_request_effect.handlers import (
    HandlerDevRollDecidedObserved,
    HandlerDevRollQueuedRoll,
)
from omnimarket.nodes.node_dev_roll_decision_request_effect.models import (
    EnumDevRollDecisionRequestSkipReason,
    ModelDevRollDecisionRequestSkipped,
    ModelDevRollObservationRecorded,
    ModelDevRollObservationState,
)
from omnimarket.nodes.node_dev_roll_request_compute.handlers import decide_dev_roll
from omnimarket.nodes.node_dev_roll_request_compute.models import (
    EnumDevRollAction,
    ModelDevRollDecided,
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

# The batching window the compute policy is read with: the decided event is merged
# _EVENT_AGE_SECONDS before NOW, so the window is closed and the decide rolls now.
_WINDOW_SECONDS = 30
_EVENT_AGE_SECONDS = 60


@pytest.fixture
def compute_policy(tmp_path: Path) -> ModelDevRollPolicy:
    """The compute node's batching policy, read through its overlay, with a closed window."""
    doc = {
        **COMPUTE_POLICY_DOC,
        "batch_window_seconds": _WINDOW_SECONDS,
        "known_repositories": ["omnimarket"],
        "build_context_repository": "omnimarket",
    }
    return load_dev_roll_policy(write_compute_overlay(tmp_path, doc))


class _UnreadableStore:
    """A store whose load always raises OSError."""

    def __init__(self) -> None:
        self._state = ModelDevRollObservationState()

    def load(self) -> ModelDevRollObservationState:
        raise OSError("the observation state is unreadable")

    def save(self, state: ModelDevRollObservationState) -> None:
        self._state = state


class _UnwritableStore:
    """A store whose save always raises OSError."""

    def __init__(self) -> None:
        self._state = ModelDevRollObservationState()

    def load(self) -> ModelDevRollObservationState:
        return self._state

    def save(self, state: ModelDevRollObservationState) -> None:
        raise OSError("the observation state is unwritable")


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


def _skip(broker) -> ModelDevRollDecisionRequestSkipped:
    (message,) = on(broker, SKIPPED_TOPIC)
    return ModelDevRollDecisionRequestSkipped.model_validate(message.payload)


def _roll_now(policy: ModelDevRollPolicy) -> ModelDevRollDecided:
    """One roll_now decision over one pending event older than the batching window."""
    decided = decide_dev_roll(
        compute_facts(
            (compute_tip("omnimarket", age_seconds=_EVENT_AGE_SECONDS),), None
        ),
        policy,
    )
    assert isinstance(decided, ModelDevRollDecided)
    assert decided.action is EnumDevRollAction.ROLL_NOW
    return decided


async def test_a_manifest_with_no_job_id_is_skipped_job_id_missing(store) -> None:
    payload = manifest_payload(job_id="job-1")
    del payload["job_id"]
    dispatch, transport, broker, _topics = chain(store)
    await _send_manifest(transport, payload)
    assert await dispatch.drain() == 1
    skip = _skip(broker)
    assert skip.reason is EnumDevRollDecisionRequestSkipReason.JOB_ID_MISSING
    assert skip.reason is not EnumDevRollDecisionRequestSkipReason.ALREADY_REQUESTED
    assert skip.job_id is None
    assert "job_id" in (skip.detail or "")
    assert on(broker, FACTS_TOPIC) == []


async def test_an_issuer_outside_the_ci_pattern_is_skipped(store) -> None:
    dispatch, transport, broker, _topics = chain(store)
    await _send_manifest(
        transport, manifest_payload(job_id="job-1", issuer="someone-else")
    )
    assert await dispatch.drain() == 1
    skip = _skip(broker)
    assert skip.reason is EnumDevRollDecisionRequestSkipReason.ISSUER_NOT_A_CI_REQUEST
    assert skip.job_id == "job-1"
    assert "someone-else" in (skip.detail or "")
    assert on(broker, FACTS_TOPIC) == []


async def test_a_git_ref_that_is_not_a_sha_is_skipped(store) -> None:
    dispatch, transport, broker, _topics = chain(store)
    await _send_manifest(
        transport, manifest_payload(job_id="job-1", git_ref="origin/dev")
    )
    assert await dispatch.drain() == 1
    skip = _skip(broker)
    assert skip.reason is EnumDevRollDecisionRequestSkipReason.GIT_REF_NOT_A_SHA
    assert skip.job_id == "job-1"
    assert "origin/dev" in (skip.detail or "")
    assert on(broker, FACTS_TOPIC) == []


async def test_a_manifest_with_no_usable_instant_is_skipped(store) -> None:
    dispatch, transport, broker, _topics = chain(store)
    await _send_manifest(
        transport,
        manifest_payload(job_id="job-1", requested_at=None, accepted_at=None),
    )
    assert await dispatch.drain() == 1
    skip = _skip(broker)
    assert skip.reason is EnumDevRollDecisionRequestSkipReason.TIME_MISSING
    assert skip.job_id == "job-1"
    assert on(broker, FACTS_TOPIC) == []


async def test_the_same_job_id_twice_yields_facts_then_already_requested(store) -> None:
    payload = manifest_payload(job_id="job-1")
    dispatch, transport, broker, _topics = chain(store)
    await _send_manifest(transport, payload)
    await _send_manifest(transport, payload)
    assert await dispatch.drain() == 2
    (facts_message,) = on(broker, FACTS_TOPIC)
    assert facts_message.payload["pending_events"] is not None
    skip = _skip(broker)
    assert skip.reason is EnumDevRollDecisionRequestSkipReason.ALREADY_REQUESTED
    assert skip.job_id == "job-1"
    assert len(on(broker, FACTS_TOPIC)) == 1
    assert len(on(broker, SKIPPED_TOPIC)) == 1


async def test_an_unreadable_store_is_skipped_node_unavailable(store) -> None:
    bad = _UnreadableStore()
    dispatch, transport, broker, _topics = chain_with(
        HandlerDevRollQueuedRoll(bad, policy=POLICY, clock=lambda: NOW),
        HandlerDevRollDecidedObserved(bad, policy=POLICY),
    )
    await _send_manifest(transport, manifest_payload(job_id="job-1"))
    assert await dispatch.drain() == 1
    skip = _skip(broker)
    assert skip.reason is EnumDevRollDecisionRequestSkipReason.NODE_UNAVAILABLE
    assert skip.reason is not EnumDevRollDecisionRequestSkipReason.TIME_MISSING
    assert "the observation state is unreadable" in (skip.detail or "")
    assert on(broker, FACTS_TOPIC) == []


async def test_a_handler_with_no_overlay_is_skipped_node_unavailable(store) -> None:
    # The handler built the way the runtime builds it over an environ with no overlay root.
    dispatch, transport, broker, _topics = chain_with(
        HandlerDevRollQueuedRoll(store, environ={}),
        HandlerDevRollDecidedObserved(store, policy=POLICY),
    )
    await _send_manifest(transport, manifest_payload(job_id="job-1"))
    assert await dispatch.drain() == 1
    skip = _skip(broker)
    assert skip.reason is EnumDevRollDecisionRequestSkipReason.NODE_UNAVAILABLE
    assert "ONEX_SKILL_OVERLAY_ROOTS" in (skip.detail or "")
    assert on(broker, FACTS_TOPIC) == []


async def test_a_roll_now_whose_store_cannot_save_records_the_error(
    compute_policy: ModelDevRollPolicy,
) -> None:
    bad = _UnwritableStore()
    dispatch, transport, broker, _topics = chain_with(
        HandlerDevRollQueuedRoll(bad, policy=POLICY, clock=lambda: NOW),
        HandlerDevRollDecidedObserved(bad, policy=POLICY),
    )
    await _send_decided(transport, _roll_now(compute_policy))
    assert await dispatch.drain() == 1
    (recorded_message,) = on(broker, RECORDED_TOPIC)
    recorded = ModelDevRollObservationRecorded.model_validate(recorded_message.payload)
    assert recorded.recorded is False
    assert "the observation state is unwritable" in (recorded.error or "")
    assert recorded.cleared_count == 0
    assert recorded.action == "roll_now"


class _SkipsEverything(HandlerDevRollQueuedRoll):
    """Failing control: a queued-roll handler that skips every manifest."""

    def handle(self, request) -> ModelDevRollDecisionRequestSkipped:
        return ModelDevRollDecisionRequestSkipped(
            job_id=request.job_id,
            reason=EnumDevRollDecisionRequestSkipReason.NODE_UNAVAILABLE,
            detail="failing control: every manifest is skipped",
        )


async def test_failing_control_a_queued_roll_that_skips_everything_yields_no_facts(
    store,
) -> None:
    """The golden expectation (one facts message) is red against this stand-in."""
    dispatch, transport, broker, _topics = chain_with(
        _SkipsEverything(store, policy=POLICY, clock=lambda: NOW),
        HandlerDevRollDecidedObserved(store, policy=POLICY),
    )
    await _send_manifest(transport, manifest_payload(job_id="job-1"))
    assert await dispatch.drain() == 1
    assert on(broker, FACTS_TOPIC) == []
    skip = _skip(broker)
    assert skip.reason is EnumDevRollDecisionRequestSkipReason.NODE_UNAVAILABLE
