# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The topic reaper's lifecycle, guards and policy refusal, on an injected clock (OMN-21594)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from omnimarket.models.model_topic_reaper import (
    REAPER_POLICY_MISSING,
    EnumReaperState,
    ModelReaperObservation,
    ModelReaperTopicState,
    ModelTopicReaperPolicy,
    ModelTopicReaperRequest,
    ModelTopicReaperResult,
)
from omnimarket.nodes.node_topic_reaper_compute.handlers.handler_topic_reaper import (
    HandlerTopicReaper,
)
from omnimarket.nodes.node_topic_reaper_compute.overlay import load_reaper_policy

pytestmark = pytest.mark.unit

DAY0 = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
TOPIC = "onex.evt.example.stale.v1"


def _policy(**over: object) -> ModelTopicReaperPolicy:
    values: dict[str, object] = {
        "quiet_days_before_flag": 14,
        "flag_to_delete_days": 3,
        "quiet_observations_required": 2,
        "enforce": True,
        "protected_prefixes": ("_",),
        "baseline_topics": ("onex.cmd.baseline.control.v1",),
    }
    values.update(over)
    return ModelTopicReaperPolicy.model_validate(values)


def _obs(topic: str = TOPIC, **over: object) -> ModelReaperObservation:
    values: dict[str, object] = {
        "topic": topic,
        "partitions": 3,
        "topic_id": f"id-{topic}",
        "end_offset_total": 10,
        "last_record_at": DAY0 - timedelta(days=30),
        "group_members": 0,
        "committed_groups": (),
    }
    values.update(over)
    return ModelReaperObservation.model_validate(values)


class _Run:
    """One topic's record carried from observation to observation, the way a caller stores it."""

    def __init__(
        self, policy: ModelTopicReaperPolicy | None, *, boot: tuple[str, ...] = ()
    ) -> None:
        self.handler = HandlerTopicReaper()
        self.policy = policy
        self.boot = boot
        self.state: tuple[ModelReaperTopicState, ...] = ()

    def at(
        self, day: float, *obs: ModelReaperObservation, **over: object
    ) -> ModelTopicReaperResult:
        request = ModelTopicReaperRequest.model_validate(
            {
                "now": DAY0 + timedelta(days=day),
                "policy": self.policy,
                "observations": obs or (_obs(),),
                "state": self.state,
                "boot_set_topics": self.boot,
                **over,
            }
        )
        result = self.handler.handle(request)
        self.state = result.state
        return result

    def state_of(self, topic: str = TOPIC) -> EnumReaperState | None:
        return next((r.state for r in self.state if r.topic == topic), None)


def test_withdrawn_topic_flagged_then_deleted_by_id_at_day_three() -> None:
    run = _Run(_policy())
    day0 = run.at(0)
    assert run.state_of() is EnumReaperState.FLAGGED
    assert day0.delete_targets == ()
    assert run.at(1).delete_targets == ()
    day2 = run.at(2)
    assert day2.delete_targets == ()
    assert run.state_of() is EnumReaperState.FLAGGED  # age_out does not fire at day two
    day3 = run.at(3)
    assert [(t.topic, t.topic_id) for t in day3.delete_targets] == [
        (TOPIC, f"id-{TOPIC}")
    ]
    assert run.state_of() is EnumReaperState.DELETING


def test_re_announcement_on_day_two_clears_the_flag_and_resets_the_clock() -> None:
    run = _Run(_policy())
    run.at(0)
    run.at(1)
    cleared = run.at(2, reannounced_topics=(TOPIC,))
    assert run.state_of() is EnumReaperState.ACTIVE
    assert cleared.decisions[0].reason == "re_announced"
    assert run.state[0].flagged_at is None
    # Quiet again from day three: a fresh flag, so nothing is deletable at day three or five.
    run.at(3)
    assert run.state_of() is EnumReaperState.FLAGGED
    assert run.at(5).delete_targets == ()
    assert run.at(6).delete_targets != ()


def test_the_quiet_observation_count_is_required_not_just_elapsed_days() -> None:
    run = _Run(_policy(quiet_observations_required=3))
    run.at(0)  # the flagging observation is the first quiet one
    late = run.at(5)  # five days elapsed, but only two quiet observations
    assert late.delete_targets == ()
    assert run.state_of() is EnumReaperState.FLAGGED
    assert run.at(6).delete_targets != ()


def test_live_group_member_is_never_handed_out_and_restarts_the_clock() -> None:
    run = _Run(_policy())
    run.at(0)
    run.at(2)
    busy = run.at(3, _obs(group_members=1))
    assert busy.delete_targets == ()
    assert run.state_of() is EnumReaperState.FLAGGED
    assert run.state[0].flagged_at == DAY0 + timedelta(days=3)
    assert run.at(5).delete_targets == ()  # two days after the activity: too early
    assert run.at(6).delete_targets != ()


def test_moving_end_offsets_return_an_eligible_topic_to_flagged() -> None:
    run = _Run(_policy(enforce=False))  # report-only keeps the record eligible
    run.at(0)
    run.at(2)
    deletable = run.at(3)
    assert deletable.deletable_topics == 1
    assert run.state_of() is EnumReaperState.ELIGIBLE
    moved = run.at(
        4, _obs(end_offset_total=11, last_record_at=DAY0 + timedelta(days=4))
    )
    assert moved.delete_targets == ()
    assert moved.deletable_topics == 0
    assert run.state_of() is EnumReaperState.FLAGGED
    assert moved.decisions[0].reason == "activity_seen"


def test_committed_group_with_unread_commit_time_counts_as_activity() -> None:
    run = _Run(_policy())
    held = run.at(0, _obs(committed_groups=("group.a",)))
    assert run.state_of() is EnumReaperState.ACTIVE
    assert held.decisions[0].reason == "active_not_quiet"


def test_recent_commit_keeps_a_topic_active() -> None:
    run = _Run(_policy())
    run.at(
        0, _obs(committed_groups=("group.a",), last_commit_at=DAY0 - timedelta(days=2))
    )
    assert run.state_of() is EnumReaperState.ACTIVE


def test_missing_policy_reports_and_deletes_nothing() -> None:
    run = _Run(None)
    result = run.at(0)
    for day in (3, 30, 400):
        result = run.at(day)
        assert result.delete_targets == ()
    assert result.status == "policy_missing"
    assert result.report_code == REAPER_POLICY_MISSING
    assert result.state == ()


def test_no_overlay_means_no_policy(tmp_path: Path) -> None:
    assert (
        load_reaper_policy("dev", {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}) is None
    )


def test_overlay_without_a_complete_block_means_no_policy(tmp_path: Path) -> None:
    node_dir = tmp_path / "node_topic_reaper_compute"
    node_dir.mkdir()
    (node_dir / "overlay.yaml").write_text(
        "lanes:\n  dev:\n    flag_to_delete_days: 3\n", encoding="utf-8"
    )
    assert (
        load_reaper_policy("dev", {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}) is None
    )


def test_overlay_with_every_value_loads(tmp_path: Path) -> None:
    node_dir = tmp_path / "node_topic_reaper_compute"
    node_dir.mkdir()
    example = Path(__file__).resolve().parents[4] / (
        "src/omnimarket/nodes/node_topic_reaper_compute/overlay.example.yaml"
    )
    (node_dir / "overlay.yaml").write_text(
        example.read_text(encoding="utf-8"), encoding="utf-8"
    )
    environ = {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}
    assert (
        load_reaper_policy("another-lane", environ) is None
    )  # a lane with no block gets nothing
    policy = load_reaper_policy("example-lane", environ)
    assert policy is not None
    assert policy.flag_to_delete_days == 3
    assert policy.enforce is False


def test_policy_has_no_default_for_any_interval() -> None:
    with pytest.raises(ValueError, match="quiet_days_before_flag"):
        ModelTopicReaperPolicy.model_validate({"enforce": False})


@pytest.mark.parametrize(
    "topic", ["_schemas", "onex.cmd.baseline.control.v1"], ids=["prefix", "baseline"]
)
def test_baseline_and_protected_topics_are_never_flagged(topic: str) -> None:
    run = _Run(_policy())
    for day in (0, 3, 30):
        result = run.at(day, _obs(topic))
        assert result.delete_targets == ()
    assert run.state_of(topic) is EnumReaperState.ACTIVE
    assert result.decisions[0].reason == "protected"


def test_boot_set_contract_topic_is_never_flagged_or_deleted() -> None:
    run = _Run(_policy(), boot=(TOPIC,))
    for day in (0, 3, 60):
        result = run.at(day)
        assert result.delete_targets == ()
    assert run.state_of() is EnumReaperState.ACTIVE
    assert result.decisions[0].reason == "declared_by_boot_set"


def test_a_flag_is_cleared_when_the_boot_set_starts_declaring_the_topic() -> None:
    run = _Run(_policy())
    run.at(0)
    assert run.state_of() is EnumReaperState.FLAGGED
    run.boot = (TOPIC,)
    result = run.at(3)
    assert run.state_of() is EnumReaperState.ACTIVE
    assert result.delete_targets == ()


def test_unfinished_work_reference_blocks_the_delete_until_it_clears() -> None:
    run = _Run(_policy())
    run.at(0)
    run.at(2)
    held = run.at(3, unfinished_work_topics=(TOPIC,))
    assert held.delete_targets == ()
    assert held.decisions[0].reason == "unfinished_work_reference"
    assert run.at(10, unfinished_work_topics=(TOPIC,)).delete_targets == ()
    assert run.at(11).delete_targets != ()


def test_unread_evidence_holds_the_topic_and_never_flags_or_deletes() -> None:
    run = _Run(_policy())
    result = run.at(0, _obs(group_members=None))
    assert result.held_unread == 1
    assert result.flagged_topics == 0
    run.at(0)
    run.at(2)
    unread = run.at(3, _obs(end_offset_total=None))
    assert unread.delete_targets == ()
    assert run.state_of() is EnumReaperState.FLAGGED


def test_enforce_false_reports_deletable_but_hands_out_nothing() -> None:
    run = _Run(_policy(enforce=False))
    run.at(0)
    run.at(2)
    result = run.at(3)
    assert result.delete_targets == ()
    assert result.deletable_topics == 1
    assert result.deletable_partitions == 3
    assert run.state_of() is EnumReaperState.ELIGIBLE


def test_a_topic_without_a_recorded_id_is_never_handed_out() -> None:
    run = _Run(_policy())
    no_id = _obs(topic_id=None)
    run.at(0, no_id)
    run.at(2, no_id)
    result = run.at(3, no_id)
    assert result.delete_targets == ()
    assert result.deletable_without_topic_id == 1


def test_deleting_topic_is_confirmed_when_the_broker_no_longer_lists_it() -> None:
    run = _Run(_policy())
    run.at(0)
    run.at(2)
    run.at(3)
    assert run.state_of() is EnumReaperState.DELETING
    again = run.at(4)
    assert [t.topic for t in again.delete_targets] == [
        TOPIC
    ]  # still listed: handed out again
    gone = run.at(5, _obs("onex.evt.example.other.v1", last_record_at=DAY0))
    assert (
        next(d for d in gone.decisions if d.topic == TOPIC).reason
        == "deleted_confirmed"
    )
    assert run.state_of() is None


def test_the_age_guard_is_the_executors_not_the_handlers() -> None:
    """`age_out` runs through core's executor: the guard value alone decides the move."""
    handler = HandlerTopicReaper()
    assert (
        handler._fire(
            EnumReaperState.FLAGGED,
            "interval_and_quiet_observations_met",
            {"interval_elapsed": False},
        )
        is None
    )
    assert (
        handler._fire(
            EnumReaperState.FLAGGED,
            "interval_and_quiet_observations_met",
            {"interval_elapsed": True},
        )
        is EnumReaperState.ELIGIBLE
    )
    assert (
        handler._fire(
            EnumReaperState.ELIGIBLE,
            "deletion_issued",
            {"unfinished_work_references": True},
        )
        is None
    )
    assert (
        handler._fire(
            EnumReaperState.ELIGIBLE,
            "deletion_issued",
            {"unfinished_work_references": False},
        )
        is EnumReaperState.DELETING
    )


def test_a_never_produced_topic_starts_its_quiet_clock_at_creation() -> None:
    run = _Run(_policy())
    never = _obs(
        end_offset_total=0, last_record_at=None, created_at=DAY0 - timedelta(days=20)
    )
    run.at(0, never)
    assert run.state_of() is EnumReaperState.FLAGGED
    young = _obs(
        "onex.evt.example.young.v1",
        end_offset_total=0,
        last_record_at=None,
        created_at=DAY0 - timedelta(days=2),
    )
    run.at(0, young)
    assert run.state_of("onex.evt.example.young.v1") is EnumReaperState.ACTIVE


def test_a_produced_topic_with_unread_record_time_is_not_aged_from_its_creation() -> (
    None
):
    run = _Run(_policy())
    unknown_age = _obs(
        end_offset_total=500, last_record_at=None, created_at=DAY0 - timedelta(days=90)
    )
    run.at(0, unknown_age)
    assert run.state_of() is EnumReaperState.ACTIVE
    # Offsets unchanged across the quiet interval: now it is provably quiet.
    run.at(15, unknown_age)
    assert run.state_of() is EnumReaperState.FLAGGED
