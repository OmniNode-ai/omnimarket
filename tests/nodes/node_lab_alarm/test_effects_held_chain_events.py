# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain event tests of the lab alarm's effects-held condition (OMN-23106).

Golden: a live consumer group, or a hold under its bound, ends in the completed event and opens no lane.
Error: the named break, no live member for the bound or longer, ends in the typed failure event, opens one
lane whose brief names the condition and when it began, and opens no second lane for the same episode; a
dispatcher that cannot start the lane leaves the episode without one and the next run tries again; an
unreadable group list is INDETERMINATE and restarts the clock.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_lab_alarm.adapters.inmemory_adapters import (
    InMemoryDispatcher,
    InMemoryEffectsGroups,
    InMemoryHeldState,
)
from omnimarket.nodes.node_lab_alarm.adapters.runtime_adapters import (
    CommandLaneDispatcher,
    FileHeldState,
)
from omnimarket.nodes.node_lab_alarm.handlers.handler_effects_held import (
    CONDITION,
    HandlerEffectsHeld,
    events_for,
    load_topics,
)
from omnimarket.nodes.node_lab_alarm.models.model_lab_alarm_effects_held import (
    EnumEffectsHeldFault,
    EnumEffectsHeldOutcome,
    EnumEscalation,
    ModelEffectsGroupReading,
    ModelEffectsHeldCommand,
    ModelEscalationBrief,
)
from omnimarket.nodes.node_lab_alarm.overlay import (
    ModelEffectsHeldSettings,
    ModelLabAlarmOverlay,
)
from omnimarket.nodes.node_lab_alarm.protocols import LaneDispatchError
from omnimarket.nodes.node_lab_alarm.runtime_lab_alarm import run_effects_held

pytestmark = pytest.mark.unit

_NODE = Path(__file__).resolve().parents[3] / "src/omnimarket/nodes/node_lab_alarm"
_NOW = datetime(2026, 11, 8, 6, 0, 0, tzinfo=UTC)
_GROUP = "local.examplepkg.node_example_consumer.consume.1.2.0.__i.runtime-effects.__t.onex.cmd.example.work-requested.v1"


def _overlay() -> ModelLabAlarmOverlay:
    return ModelLabAlarmOverlay.model_validate(
        yaml.safe_load((_NODE / "overlay.example.yaml").read_text(encoding="utf-8"))
    )


def _settings() -> ModelEffectsHeldSettings:
    settings = _overlay().effects_held
    assert settings is not None
    return settings


def _stamp(seconds_ago: int) -> str:
    return (_NOW - timedelta(seconds=seconds_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _live() -> tuple[ModelEffectsGroupReading, ...]:
    return (ModelEffectsGroupReading(group=_GROUP, state="Stable", members=1),)


def _absent() -> tuple[ModelEffectsGroupReading, ...]:
    return (ModelEffectsGroupReading(group=_GROUP, state="Empty", members=0),)


def _handler(
    readings: tuple[ModelEffectsGroupReading, ...],
    state: dict[str, str] | None = None,
) -> tuple[
    HandlerEffectsHeld, InMemoryEffectsGroups, InMemoryHeldState, InMemoryDispatcher
]:
    groups = InMemoryEffectsGroups(readings)
    held = InMemoryHeldState(state)
    dispatcher = InMemoryDispatcher()
    handler = HandlerEffectsHeld(
        settings=_settings(),
        groups=groups,
        state=held,
        dispatcher=dispatcher,
        clock=lambda: _NOW,
    )
    return handler, groups, held, dispatcher


def _command(
    *, write: bool = True, fault: EnumEffectsHeldFault = EnumEffectsHeldFault.NONE
) -> ModelEffectsHeldCommand:
    return ModelEffectsHeldCommand(correlation_id="chain-1", write=write, fault=fault)


def test_golden_chain_live_consumer_completes_and_opens_no_lane() -> None:
    topics = load_topics()
    handler, _groups, held, dispatcher = _handler(
        _live(), {"held_since": _stamp(300), "escalated_at": _stamp(200)}
    )
    result = handler.handle(_command())
    events = events_for(result, topics)
    assert result.outcome is EnumEffectsHeldOutcome.OK
    assert result.escalation is EnumEscalation.NOT_NEEDED
    assert dispatcher.briefs == []
    assert held.state == {}, "a live member ends the episode and clears its lane"
    assert [t for t, _ in events] == [
        topics["effects_held_run_started"],
        topics["effects_held_group_read"],
        topics["effects_held_run_completed"],
    ]


def test_golden_chain_hold_under_the_bound_is_ok_and_keeps_the_clock() -> None:
    handler, _groups, held, dispatcher = _handler(_absent(), {"held_since": _stamp(30)})
    result = handler.handle(_command())
    assert result.outcome is EnumEffectsHeldOutcome.OK
    assert result.held_for_seconds == 30
    assert dispatcher.briefs == []
    assert held.state == {"held_since": _stamp(30)}


def test_first_absent_read_starts_the_clock_at_now() -> None:
    handler, _groups, held, dispatcher = _handler(_absent())
    result = handler.handle(_command())
    assert result.outcome is EnumEffectsHeldOutcome.OK
    assert held.state == {"held_since": _stamp(0)}
    assert dispatcher.briefs == []


def test_error_chain_absent_past_the_bound_alarms_and_opens_one_lane() -> None:
    topics = load_topics()
    handler, _groups, held, dispatcher = _handler(
        _absent(), {"held_since": _stamp(120)}
    )
    result = handler.handle(_command())
    events = events_for(result, topics)
    assert result.outcome is EnumEffectsHeldOutcome.ALARM
    assert result.escalation is EnumEscalation.DISPATCHED
    assert result.held_since == _stamp(120)
    assert result.bound_seconds == 60
    assert len(dispatcher.briefs) == 1
    brief = dispatcher.briefs[0]
    assert brief.condition == CONDITION
    assert brief.raised_at == _stamp(120)
    assert held.state["escalated_at"] == _stamp(0)
    assert [t for t, _ in events] == [
        topics["effects_held_run_started"],
        topics["effects_held_group_read"],
        topics["effects_held_lane_dispatch"],
        topics["effects_held_run_failed"],
    ]
    assert events[-1][1]["failure"] == f"alarm:{CONDITION};escalation=dispatched"


def test_the_bound_itself_alarms() -> None:
    handler, _groups, _held, dispatcher = _handler(
        _absent(), {"held_since": _stamp(60)}
    )
    assert handler.handle(_command()).outcome is EnumEffectsHeldOutcome.ALARM
    assert len(dispatcher.briefs) == 1


def test_error_chain_second_run_of_the_same_episode_opens_no_second_lane() -> None:
    handler, _groups, _held, dispatcher = _handler(
        _absent(), {"held_since": _stamp(120)}
    )
    first = handler.handle(_command())
    second = handler.handle(_command())
    assert first.escalation is EnumEscalation.DISPATCHED
    assert second.outcome is EnumEffectsHeldOutcome.ALARM
    assert second.escalation is EnumEscalation.ALREADY
    assert len(dispatcher.briefs) == 1


def test_error_chain_a_lane_that_cannot_start_fails_loud_and_is_retried() -> None:
    topics = load_topics()
    handler, _groups, held, dispatcher = _handler(
        _absent(), {"held_since": _stamp(120)}
    )
    dispatcher.refuse = "dispatch exited 3"
    failed = handler.handle(_command())
    assert failed.outcome is EnumEffectsHeldOutcome.ALARM
    assert failed.escalation is EnumEscalation.FAILED
    assert failed.reasons == ("dispatch:dispatch exited 3",)
    assert "escalated_at" not in held.state, "an episode with no lane is not escalated"
    assert "escalation=failed" in failed.failure
    assert topics["effects_held_lane_dispatch"] in [
        t for t, _ in events_for(failed, topics)
    ]
    dispatcher.refuse = None
    retried = handler.handle(_command())
    assert retried.escalation is EnumEscalation.DISPATCHED
    assert len(dispatcher.briefs) == 1


def test_error_chain_unreadable_groups_are_indeterminate_and_restart_the_clock() -> (
    None
):
    handler, groups, held, dispatcher = _handler(
        _absent(), {"held_since": _stamp(500), "escalated_at": _stamp(400)}
    )
    groups.unreadable = "unreadable (TimeoutError)"
    result = handler.handle(_command())
    assert result.outcome is EnumEffectsHeldOutcome.INDETERMINATE
    assert result.reasons == ("unreadable:unreadable (TimeoutError)",)
    assert dispatcher.briefs == []
    assert held.state == {}


def test_error_chain_no_matching_group_is_indeterminate_not_live() -> None:
    handler, _groups, _held, dispatcher = _handler(())
    result = handler.handle(_command())
    assert result.outcome is EnumEffectsHeldOutcome.INDETERMINATE
    assert result.reasons == ("no-group-matched",)
    assert dispatcher.briefs == []


def test_a_group_that_is_stable_with_no_member_is_not_live() -> None:
    handler, _groups, _held, dispatcher = _handler(
        (ModelEffectsGroupReading(group=_GROUP, state="Stable", members=0),),
        {"held_since": _stamp(90)},
    )
    assert handler.handle(_command()).outcome is EnumEffectsHeldOutcome.ALARM
    assert len(dispatcher.briefs) == 1


def test_a_probe_reports_the_alarm_and_changes_nothing() -> None:
    handler, _groups, held, dispatcher = _handler(
        _absent(), {"held_since": _stamp(120)}
    )
    result = handler.handle(_command(write=False))
    assert result.outcome is EnumEffectsHeldOutcome.ALARM
    assert result.escalation is EnumEscalation.NOT_NEEDED
    assert result.state_kept is False
    assert dispatcher.briefs == []
    assert held.state == {"held_since": _stamp(120)}


def test_the_named_break_consumer_absent_alarms_a_live_group_and_opens_no_lane() -> (
    None
):
    handler, _groups, held, dispatcher = _handler(_live())
    result = handler.handle(_command(fault=EnumEffectsHeldFault.CONSUMER_ABSENT))
    assert result.outcome is EnumEffectsHeldOutcome.ALARM
    assert result.fault is EnumEffectsHeldFault.CONSUMER_ABSENT
    assert result.escalation is EnumEscalation.DRILL
    assert result.state_kept is False
    assert dispatcher.briefs == []
    assert held.state == {}


def test_cli_alarm_exits_two_and_names_the_escalation() -> None:
    handler, _groups, _held, dispatcher = _handler(
        _absent(), {"held_since": _stamp(120)}
    )
    lines: list[str] = []
    code = run_effects_held(
        "effects-held",
        EnumEffectsHeldFault.NONE,
        overlay=_overlay(),
        handler=handler,
        out=lines.append,
    )
    assert code == 2
    assert any("escalation=dispatched" in line for line in lines)
    assert any(load_topics()["effects_held_lane_dispatch"] in line for line in lines)
    assert len(dispatcher.briefs) == 1


def test_every_effects_held_topic_the_chain_publishes_is_declared_in_the_contract() -> (
    None
):
    contract = yaml.safe_load((_NODE / "contract.yaml").read_text(encoding="utf-8"))
    declared = set(contract["externally_consumed_topics"])
    topics = load_topics()
    for role in (
        "effects_held_run_started",
        "effects_held_group_read",
        "effects_held_lane_dispatch",
        "effects_held_run_completed",
        "effects_held_run_failed",
    ):
        assert topics[role] in declared


def test_file_held_state_round_trips_and_reads_a_bad_file_as_empty(
    tmp_path: Path,
) -> None:
    store = FileHeldState(tmp_path / "state" / "held.json")
    assert store.load() == {}
    store.save({"held_since": "2026-11-08T06:00:00Z"})
    assert store.load() == {"held_since": "2026-11-08T06:00:00Z"}
    (tmp_path / "state" / "held.json").write_text("[1]", encoding="utf-8")
    assert store.load() == {}


def _brief() -> ModelEscalationBrief:
    return ModelEscalationBrief(
        condition=CONDITION,
        subject="runtime-effects",
        raised_at="2026-11-08T05:58:00Z",
        detail="no live member past the bound",
        evidence="example evidence",
    )


def _dispatcher(tmp_path: Path, code: int) -> CommandLaneDispatcher:
    settings = _settings().escalation.model_copy(
        update={
            "command": (sys.executable, "-c", f"import sys; sys.exit({code})"),
            "settle_seconds": 30.0,
            "brief_dir": str(tmp_path / "briefs"),
        }
    )
    return CommandLaneDispatcher(settings)


def test_command_dispatcher_writes_the_brief_and_accepts_a_zero_exit(
    tmp_path: Path,
) -> None:
    _dispatcher(tmp_path, 0).dispatch(_brief())
    text = (
        tmp_path / "briefs" / "effects-held-behind-runtime-202611080558-brief.md"
    ).read_text(encoding="utf-8")
    assert f"Condition: {CONDITION}" in text
    assert "Raised at: 2026-11-08T05:58:00Z" in text
    assert "never a raw container command" in text


def test_command_dispatcher_refuses_a_nonzero_exit(tmp_path: Path) -> None:
    with pytest.raises(LaneDispatchError, match="dispatch exited 3"):
        _dispatcher(tmp_path, 3).dispatch(_brief())


def test_command_dispatcher_refuses_a_command_that_does_not_exist(
    tmp_path: Path,
) -> None:
    settings = _settings().escalation.model_copy(
        update={
            "command": (str(tmp_path / "no-such-dispatcher"),),
            "brief_dir": str(tmp_path / "briefs"),
        }
    )
    with pytest.raises(LaneDispatchError, match="FileNotFoundError"):
        CommandLaneDispatcher(settings).dispatch(_brief())
