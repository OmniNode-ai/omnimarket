# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests of the handlers and stores without the bus.

Each manifest in, one decision request (or a typed skip) out: the tip event's
deterministic identity, the repository read out of the issuer's named group,
the merged instant, the seen-job bound, the shadow last roll, and what one
observed decision of the compute node clears.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4, uuid5

import pytest

from omnimarket.events.dev_roll import (
    EnumDevRollAction,
    EnumDevRollHealth,
    EnumDevRollHoldReason,
    ModelDevRollDecided,
    ModelDevRollFacts,
    ModelDevRollRequest,
    ModelDevTipChanged,
)
from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_dev_roll_decision_request_effect.handlers import (
    FileDevRollObservationStore,
    HandlerDevRollDecidedObserved,
    HandlerDevRollQueuedRoll,
    store_from_policy,
)
from omnimarket.nodes.node_dev_roll_decision_request_effect.handlers.handler_dev_roll_queued_roll import (
    NAMESPACE,
)
from omnimarket.nodes.node_dev_roll_decision_request_effect.models import (
    EnumDevRollDecisionRequestSkipReason,
    ModelDevRollDecisionRequestSkipped,
    ModelDevRollFileObservationStore,
    ModelDevRollManifestEvent,
    ModelDevRollObservationState,
)
from tests.unit.nodes.node_dev_roll_decision_request_effect.conftest import (
    NOW,
    POLICY,
    SHA,
    SHADOW_ROLL_SECONDS,
    manifest_payload,
)

pytestmark = pytest.mark.unit


def _queued(store, clock=NOW) -> HandlerDevRollQueuedRoll:
    return HandlerDevRollQueuedRoll(store, policy=POLICY, clock=lambda: clock)


def _manifest(**overrides):
    return ModelDevRollManifestEvent.model_validate(manifest_payload(**overrides))


def _facts(store, **overrides):
    """One manifest handled against this store; the facts it yielded."""
    result = _queued(store).handle(_manifest(**overrides))
    assert isinstance(result, ModelDevRollFacts)
    return result


def _tip(event_id: UUID) -> ModelDevTipChanged:
    return ModelDevTipChanged(
        event_id=event_id, repository="lane_one", sha=SHA, merged_at=NOW
    )


def _roll_now(rolled: tuple[UUID, ...]) -> ModelDevRollDecided:
    """One roll_now decision of the compute node covering ``rolled``."""
    correlation_id = uuid4()
    return ModelDevRollDecided(
        correlation_id=correlation_id,
        observed_at=NOW,
        action=EnumDevRollAction.ROLL_NOW,
        hold_reason=None,
        hold_until=None,
        shadow=True,
        would_request=ModelDevRollRequest(
            correlation_id=correlation_id,
            requested_by="test-requester",
            scope="full",
            runtime_lane="dev",
            git_ref="b" * 40,
            requested_at=NOW,
        ),
        rolled_event_ids=rolled,
        newest_sha_by_repository={"lane_one": "b" * 40},
        oldest_pending_merged_at=NOW,
        longest_wait_seconds=0,
        refused_events=(),
    )


def test_the_tip_event_carries_the_job_ids_deterministic_event_id(store) -> None:
    facts = _facts(store, job_id="job-1")
    (event,) = facts.pending_events
    assert event.event_id == uuid5(NAMESPACE, "job-1")


def test_the_tip_event_takes_the_repository_from_the_issuers_named_group(
    store,
) -> None:
    facts = _facts(store, job_id="job-1")
    (event,) = facts.pending_events
    assert event.repository == "omnimarket"
    other = _queued(store).handle(_manifest(job_id="job-2", issuer="gha/lane_two/pr-9"))
    assert isinstance(other, ModelDevRollFacts)
    event = other.pending_events[-1]
    assert event.repository == "lane_two"


def test_the_tip_event_merges_at_the_requested_at(store) -> None:
    facts = _facts(store, job_id="job-1")
    (event,) = facts.pending_events
    assert event.merged_at == NOW


def test_the_tip_event_merges_at_the_accepted_at_when_requested_at_is_null(
    store,
) -> None:
    facts = _facts(store, job_id="job-1", requested_at=None)
    (event,) = facts.pending_events
    assert event.merged_at == NOW + timedelta(seconds=5)


def test_the_facts_correlation_id_is_derived_from_the_job_id(store) -> None:
    facts = _facts(store, job_id="job-1")
    assert facts.correlation_id == uuid5(NAMESPACE, "request/job-1")


def test_a_naive_instant_is_time_missing(store) -> None:
    skipped = _queued(store).handle(
        _manifest(job_id="job-1", requested_at="2026-11-10T12:00:00")
    )
    assert isinstance(skipped, ModelDevRollDecisionRequestSkipped)
    assert skipped.reason is EnumDevRollDecisionRequestSkipReason.TIME_MISSING
    assert skipped.job_id == "job-1"
    also_naive_accepted = _queued(store).handle(
        _manifest(job_id="job-2", requested_at=None, accepted_at="2026-11-10T12:00:00")
    )
    assert isinstance(also_naive_accepted, ModelDevRollDecisionRequestSkipped)
    assert (
        also_naive_accepted.reason is EnumDevRollDecisionRequestSkipReason.TIME_MISSING
    )


def test_a_job_already_requested_is_skipped_already_requested(store) -> None:
    first = _queued(store).handle(_manifest(job_id="job-1"))
    assert isinstance(first, ModelDevRollFacts)
    second = _queued(store).handle(_manifest(job_id="job-1"))
    assert isinstance(second, ModelDevRollDecisionRequestSkipped)
    assert second.reason is EnumDevRollDecisionRequestSkipReason.ALREADY_REQUESTED
    assert second.job_id == "job-1"


def test_seen_job_ids_are_trimmed_to_the_policy_bound(store) -> None:
    policy = POLICY.model_copy(update={"seen_job_ids_kept": 2})
    handler = HandlerDevRollQueuedRoll(store, policy=policy, clock=lambda: NOW)
    for n in range(1, 5):
        result = handler.handle(_manifest(job_id=f"job-{n}"))
        assert isinstance(result, ModelDevRollFacts)
    assert store.load().seen_job_ids == ("job-3", "job-4")
    again = handler.handle(_manifest(job_id="job-1"))
    assert isinstance(again, ModelDevRollFacts), "the trimmed job id is accepted again"
    assert store.load().seen_job_ids == ("job-4", "job-1")


def test_a_manifest_whose_issuer_names_an_unknown_repository_is_not_skipped(
    store,
) -> None:
    """The effect node never refuses a repository; the compute node does, on the decision."""
    result = _queued(store).handle(
        _manifest(job_id="job-1", issuer="gha/mystery_lane/pr-99")
    )
    assert not isinstance(result, ModelDevRollDecisionRequestSkipped)
    (event,) = result.pending_events
    assert event.repository == "mystery_lane"


def test_the_last_roll_is_none_with_an_empty_state(store) -> None:
    assert _facts(store, job_id="job-1").last_roll is None


def test_inside_the_shadow_duration_the_last_roll_is_unfinished_and_unknown(
    store,
) -> None:
    started = NOW - timedelta(seconds=120)
    store.save(
        ModelDevRollObservationState(
            last_roll_job_id="prev-job",
            last_roll_git_ref="b" * 40,
            last_roll_started_at=started,
        )
    )
    last_roll = _facts(store, job_id="job-1").last_roll
    assert last_roll is not None
    assert last_roll.job_id == "prev-job"
    assert last_roll.git_ref == "b" * 40
    assert last_roll.runtime_started_at == started
    assert last_roll.finished_at is None
    assert last_roll.health is EnumDevRollHealth.UNKNOWN
    assert last_roll.healthy_since is None


def test_past_the_shadow_duration_the_last_roll_is_finished_and_healthy(store) -> None:
    started = NOW - timedelta(seconds=SHADOW_ROLL_SECONDS + 120)
    store.save(
        ModelDevRollObservationState(
            last_roll_job_id="prev-job",
            last_roll_git_ref="b" * 40,
            last_roll_started_at=started,
        )
    )
    last_roll = _facts(store, job_id="job-1").last_roll
    assert last_roll is not None
    assert last_roll.finished_at == started + timedelta(seconds=SHADOW_ROLL_SECONDS)
    assert last_roll.finished_at == NOW - timedelta(seconds=120)
    assert last_roll.healthy_since == last_roll.finished_at
    assert last_roll.health is EnumDevRollHealth.HEALTHY


def test_a_roll_now_clears_its_covered_events_and_records_the_last_roll(store) -> None:
    first = _tip(uuid5(NAMESPACE, "job-1"))
    second = _tip(uuid5(NAMESPACE, "job-2"))
    store.save(
        ModelDevRollObservationState(
            pending_events=(first, second), seen_job_ids=("job-1", "job-2")
        )
    )
    decided = _roll_now((first.event_id,))
    record = HandlerDevRollDecidedObserved(store, policy=POLICY).handle(decided)
    assert record.correlation_id == decided.correlation_id
    assert record.action == "roll_now"
    assert record.cleared_count == 1
    assert record.recorded is True
    assert record.error is None
    state = store.load()
    assert state.pending_events == (second,)
    assert state.seen_job_ids == ("job-1", "job-2")
    assert state.last_roll_job_id == str(decided.correlation_id)
    assert state.last_roll_git_ref == decided.would_request.git_ref
    assert state.last_roll_started_at == NOW


def test_a_hold_decision_changes_nothing(store) -> None:
    first = _tip(uuid5(NAMESPACE, "job-1"))
    before = ModelDevRollObservationState(
        pending_events=(first,), seen_job_ids=("job-1",)
    )
    store.save(before)
    decided = ModelDevRollDecided(
        correlation_id=uuid4(),
        observed_at=NOW,
        action=EnumDevRollAction.HOLD,
        hold_reason=EnumDevRollHoldReason.WINDOW_OPEN,
        hold_until=None,
        shadow=True,
        would_request=None,
        rolled_event_ids=(),
        newest_sha_by_repository={},
        oldest_pending_merged_at=None,
        longest_wait_seconds=0,
        refused_events=(),
    )
    record = HandlerDevRollDecidedObserved(store, policy=POLICY).handle(decided)
    assert record.correlation_id == decided.correlation_id
    assert record.action == "hold"
    assert record.cleared_count == 0
    assert record.recorded is True
    assert record.error is None
    assert store.load() == before


def test_the_file_store_round_trips_the_whole_state(tmp_path: Path) -> None:
    store = FileDevRollObservationStore(tmp_path)
    state = ModelDevRollObservationState(
        pending_events=(_tip(uuid5(NAMESPACE, "job-1")),),
        seen_job_ids=("job-1",),
        last_roll_job_id="prev-job",
        last_roll_git_ref="b" * 40,
        last_roll_started_at=NOW,
    )
    store.save(state)
    assert store.load() == state


def test_the_file_store_reads_an_absent_state_as_empty(tmp_path: Path) -> None:
    assert (
        FileDevRollObservationStore(tmp_path).load() == ModelDevRollObservationState()
    )


def test_the_file_store_refuses_a_corrupt_state_file(tmp_path: Path) -> None:
    store = FileDevRollObservationStore(tmp_path)
    store.save(ModelDevRollObservationState())
    (path,) = tmp_path.glob("*.json")
    path.write_text("not json at all", encoding="utf-8")
    with pytest.raises(ValueError, match="unreadable"):
        store.load()
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="not a JSON object"):
        store.load()
    path.write_text(json.dumps({"seen_job_ids": 3}), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid for"):
        store.load()


def test_store_from_policy_builds_the_file_store_under_the_home_env(
    tmp_path: Path,
) -> None:
    home = tmp_path / "state-home"
    policy = POLICY.model_copy(
        update={
            "store": ModelDevRollFileObservationStore(
                backend="file",
                home_env="EXAMPLE_STATE_DIR",
                relpath="dev-roll-decision-request",
            )
        }
    )
    store = store_from_policy(policy, {"EXAMPLE_STATE_DIR": str(home)})
    assert isinstance(store, FileDevRollObservationStore)
    state = ModelDevRollObservationState(seen_job_ids=("job-1",))
    store.save(state)
    assert store.load() == state
    assert (home / "dev-roll-decision-request").is_dir()


def test_store_from_policy_with_the_home_env_missing_raises_node_overlay_error() -> (
    None
):
    policy = POLICY.model_copy(
        update={
            "store": ModelDevRollFileObservationStore(
                backend="file",
                home_env="EXAMPLE_STATE_DIR",
                relpath="dev-roll-decision-request",
            )
        }
    )
    with pytest.raises(NodeOverlayError) as excinfo:
        store_from_policy(policy, {})
    assert "EXAMPLE_STATE_DIR" in str(excinfo.value)


class _PublishesNothing(HandlerDevRollQueuedRoll):
    """Failing control: a queued-roll handler that publishes nothing."""

    def handle(self, request):
        return None


def test_failing_control_a_queued_roll_that_publishes_nothing_yields_no_facts(
    store,
) -> None:
    """The table expectation (facts whose tip event is uuid5(NAMESPACE, job_id)) is red against this stand-in."""
    result = _PublishesNothing(store, policy=POLICY, clock=lambda: NOW).handle(
        _manifest(job_id="job-1")
    )
    assert result is None
    assert not isinstance(result, ModelDevRollFacts)


class _AnswersTwice(HandlerDevRollQueuedRoll):
    """Failing control: a queued-roll handler that answers the same manifest twice."""

    def handle(self, request):
        super().handle(request)
        return super().handle(request)


def test_failing_control_a_queued_roll_that_answers_twice_answers_a_skip(store) -> None:
    """The golden expectation (one facts result) is red against this stand-in: the second answer sees the saved job id."""
    result = _AnswersTwice(store, policy=POLICY, clock=lambda: NOW).handle(
        _manifest(job_id="job-1")
    )
    assert isinstance(result, ModelDevRollDecisionRequestSkipped)
    assert result.reason is EnumDevRollDecisionRequestSkipReason.ALREADY_REQUESTED
