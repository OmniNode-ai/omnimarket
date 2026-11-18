# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The growth rule of the broker lag condition: a group alarms only after persist_runs growth samples in a row.

A dev runtime redeploy rebalances the consumers and grows a group's lag for a run or two; a stuck group keeps
growing. Control: with no ``lag.growth`` block one growth sample alarms at once, as before. Golden: persistent
growth ends in the typed failure event on the run that reaches the count. Error: an unreadable group stays
INDETERMINATE and saves no streaks; a drill never writes them.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_lab_alarm.adapters.inmemory_adapters import (
    InMemoryGrowthStreaks,
    InMemoryLag,
    InMemoryManifest,
    InMemoryRunRecords,
    InMemorySamples,
)
from omnimarket.nodes.node_lab_alarm.adapters.runtime_adapters import (
    FileGrowthStreaks,
)
from omnimarket.nodes.node_lab_alarm.handlers.handler_broker_lag import (
    HandlerBrokerLag,
    events_for,
    load_topics,
    parse_command,
)
from omnimarket.nodes.node_lab_alarm.models.model_lab_alarm import (
    EnumBrokerLagOutcome,
    EnumLabAlarmFault,
    ModelBrokerLagCommand,
)
from omnimarket.nodes.node_lab_alarm.overlay import ModelLabAlarmOverlay

pytestmark = pytest.mark.unit

_NODE = Path(__file__).resolve().parents[3] / "src/omnimarket/nodes/node_lab_alarm"
_TOPIC = "onex.evt.example.one.v1"
_GROUP = (
    f"local.examplepkg.projection_example.consume.1.2.0.__i.runtime-main.__t.{_TOPIC}"
)
_GROWTH = {"min_growth": 5, "persist_runs": 3, "streak_path": "streaks.json"}


def _overlay(growth: dict[str, object] | None = None) -> ModelLabAlarmOverlay:
    raw = yaml.safe_load((_NODE / "overlay.example.yaml").read_text(encoding="utf-8"))
    if growth is not None:
        raw["lag"]["growth"] = growth
    return ModelLabAlarmOverlay.model_validate(raw)


def _manifest() -> dict[str, object]:
    return {
        "contracts": [
            {
                "name": "projection_example",
                "package_name": "examplepkg",
                "contract_version": "1.2.0",
                "event_bus": {"subscribe_topics": [_TOPIC]},
            }
        ]
    }


class _Rig:
    def __init__(
        self,
        growth: dict[str, object] | None = _GROWTH,
        start: int = 100,
        streaks: InMemoryGrowthStreaks | None = None,
    ) -> None:
        self.lag = InMemoryLag({_GROUP: start})
        self.samples = InMemorySamples({_GROUP: start})
        self.streaks = streaks or InMemoryGrowthStreaks()
        self.handler = HandlerBrokerLag(
            overlay=_overlay(growth),
            manifest=InMemoryManifest(_manifest()),
            lag=self.lag,
            samples=self.samples,
            records=InMemoryRunRecords(),
            clock=lambda: datetime(2026, 11, 12, 4, 0, tzinfo=UTC),
            streaks=self.streaks,
        )

    def run(self, lag: int, fault: EnumLabAlarmFault = EnumLabAlarmFault.NONE):
        self.lag.lags[_GROUP] = lag
        command = parse_command(
            json.dumps({"correlation_id": "growth-1", "fault": fault.value}).encode()
        )
        assert isinstance(command, ModelBrokerLagCommand)
        return self.handler.handle(command)


def test_control_without_a_growth_block_one_growth_sample_alarms_at_once() -> None:
    rig = _Rig(growth=None)
    result = rig.run(101)
    assert result.outcome is EnumBrokerLagOutcome.ALARM
    assert result.alarms == (f"{_GROUP}: lag grew 100->101",)
    assert rig.streaks.streaks == {}


def test_one_growth_sample_waits_and_the_streak_is_kept() -> None:
    rig = _Rig()
    result = rig.run(110)
    assert result.outcome is EnumBrokerLagOutcome.OK
    assert result.alarms == ()
    assert any("run 1 of 3" in r for r in result.reasons)
    assert rig.streaks.streaks == {_GROUP: 1}


def test_golden_chain_three_growth_samples_in_a_row_alarm_on_the_third() -> None:
    rig = _Rig()
    assert rig.run(110).outcome is EnumBrokerLagOutcome.OK
    assert rig.run(120).outcome is EnumBrokerLagOutcome.OK
    third = rig.run(130)
    assert third.outcome is EnumBrokerLagOutcome.ALARM
    assert third.alarms == (f"{_GROUP}: lag grew 120->130 for 3 samples in a row",)
    events = events_for(third, load_topics())
    assert events[-1][0] == load_topics()["broker_lag_run_failed"]


def test_a_sample_that_did_not_grow_resets_the_streak() -> None:
    rig = _Rig()
    rig.run(110)
    rig.run(120)
    assert rig.run(120).outcome is EnumBrokerLagOutcome.OK
    assert rig.streaks.streaks == {}
    rig.run(130)
    assert rig.run(140).outcome is EnumBrokerLagOutcome.OK


def test_growth_under_min_growth_does_not_count() -> None:
    rig = _Rig()
    for lag in (101, 102, 103, 104):
        assert rig.run(lag).outcome is EnumBrokerLagOutcome.OK
    assert rig.streaks.streaks == {}


def test_error_chain_an_unreadable_group_is_indeterminate_and_saves_no_streaks() -> (
    None
):
    rig = _Rig(streaks=InMemoryGrowthStreaks({_GROUP: 2}))
    rig.lag.refused[_GROUP] = "refused DESCRIBE (GroupAuthorizationFailedError)"
    result = rig.run(130)
    assert result.outcome is EnumBrokerLagOutcome.INDETERMINATE
    assert rig.streaks.streaks == {_GROUP: 2}


def test_a_planted_lag_drill_alarms_on_the_first_run_and_writes_no_streaks() -> None:
    rig = _Rig()
    result = rig.run(100, EnumLabAlarmFault.PLANTED_LAG)
    assert result.outcome is EnumBrokerLagOutcome.ALARM
    assert rig.streaks.streaks == {}


def test_a_vanished_group_still_alarms_at_once() -> None:
    rig = _Rig()
    rig.lag.never_committed.add(_GROUP)
    result = rig.run(100)
    assert result.outcome is EnumBrokerLagOutcome.ALARM
    assert "offsets vanished" in result.alarms[0]


def test_file_streaks_round_trip_and_tolerate_a_missing_or_corrupt_file(
    tmp_path: Path,
) -> None:
    store = FileGrowthStreaks(tmp_path / "state" / "streaks.json")
    assert store.load() == {}
    store.save({_GROUP: 2})
    assert store.load() == {_GROUP: 2}
    (tmp_path / "state" / "streaks.json").write_text("{not json", encoding="utf-8")
    assert store.load() == {}


def test_a_declared_growth_rule_needs_a_streak_store() -> None:
    with pytest.raises(ValueError, match="streak store"):
        HandlerBrokerLag(
            overlay=_overlay(_GROWTH),
            manifest=InMemoryManifest(_manifest()),
            lag=InMemoryLag({}),
            samples=InMemorySamples(),
            records=InMemoryRunRecords(),
            clock=lambda: datetime(2026, 11, 12, 4, 0, tzinfo=UTC),
        )


def test_the_overlay_refuses_a_zero_persist_runs() -> None:
    with pytest.raises(ValidationError):
        _overlay({**_GROWTH, "persist_runs": 0})
