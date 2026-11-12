# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seam between the broker lag verdict and the alarm delivery (OMN-23906).

The broker lag handler hands each run's verdict to node_lab_alarm_deliver_effect as a typed request. Golden: a
new alarm is posted once and appended once, and the next run of the same alarm delivers nothing (the failing
control). Seam: every alarm line the handler can produce splits into a subject that is a declared group, and a
line that changes shape is refused. Error: a delivery that fails names itself in the run's failure and the run
exits as an alarm; a probe and a drill deliver nothing; an overlay with no ``alarm_delivery`` block loads and
delivers nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_lab_alarm.adapters.inmemory_adapters import (
    InMemoryGrowthStreaks,
    InMemoryLag,
    InMemoryManifest,
    InMemoryRunRecords,
    InMemorySamples,
)
from omnimarket.nodes.node_lab_alarm.handlers.handler_broker_lag import (
    HandlerBrokerLag,
    events_for,
    load_topics,
    typed_alarms,
)
from omnimarket.nodes.node_lab_alarm.models.model_lab_alarm import (
    EnumBrokerLagOutcome,
    EnumLabAlarmFault,
    ModelBrokerLagCommand,
)
from omnimarket.nodes.node_lab_alarm.overlay import ModelLabAlarmOverlay
from omnimarket.nodes.node_lab_alarm.runtime_lab_alarm import _delivery_for, run
from omnimarket.nodes.node_lab_alarm_deliver_effect.adapters.inmemory_adapters import (
    InMemoryAlarmChannel,
    InMemoryAlarmLedger,
    InMemoryDeliveryState,
)
from omnimarket.nodes.node_lab_alarm_deliver_effect.handlers.handler_lab_alarm_deliver import (
    HandlerLabAlarmDeliver,
)
from omnimarket.nodes.node_lab_alarm_deliver_effect.overlay import ModelDeliveryOverlay

pytestmark = pytest.mark.unit

_NODE = Path(__file__).resolve().parents[3] / "src/omnimarket/nodes/node_lab_alarm"
_DELIVER = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_lab_alarm_deliver_effect"
)
_TOPIC = "onex.evt.example.one.v1"
_GROUP = (
    f"local.examplepkg.projection_example.consume.1.2.0.__i.runtime-main.__t.{_TOPIC}"
)
_CLOCK = datetime(2026, 11, 12, 4, 0, tzinfo=UTC)


def _overlay(
    *,
    delivery: dict[str, object] | None = None,
    growth: dict[str, object] | None = None,
) -> ModelLabAlarmOverlay:
    raw = yaml.safe_load((_NODE / "overlay.example.yaml").read_text(encoding="utf-8"))
    if delivery is not None:
        raw["alarm_delivery"] = delivery
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
        self, *, deliver: bool = True, growth: dict[str, object] | None = None
    ):
        self.lag = InMemoryLag({_GROUP: 100})
        self.channel = InMemoryAlarmChannel()
        self.ledger = InMemoryAlarmLedger()
        delivery = HandlerLabAlarmDeliver(
            overlay=ModelDeliveryOverlay.model_validate(
                yaml.safe_load(
                    (_DELIVER / "overlay.example.yaml").read_text(encoding="utf-8")
                )
            ),
            channel=self.channel,
            ledger=self.ledger,
            state=InMemoryDeliveryState(),
            clock=lambda: _CLOCK,
        )
        self.overlay = _overlay(
            delivery={"enabled": True} if deliver else None, growth=growth
        )
        self.handler = HandlerBrokerLag(
            overlay=self.overlay,
            manifest=InMemoryManifest(_manifest()),
            lag=self.lag,
            samples=InMemorySamples({_GROUP: 100}),
            records=InMemoryRunRecords(),
            clock=lambda: _CLOCK,
            streaks=None if growth is None else InMemoryGrowthStreaks(),
            delivery=delivery if deliver else None,
        )

    def run(
        self,
        lag: int,
        *,
        write: bool = True,
        fault: EnumLabAlarmFault = EnumLabAlarmFault.NONE,
    ):
        self.lag.lags[_GROUP] = lag
        return self.handler.handle(
            ModelBrokerLagCommand(correlation_id="wire-1", write=write, fault=fault)
        )


def test_a_new_alarm_is_posted_once_and_appended_once() -> None:
    rig = _Rig()
    result = rig.run(105)
    assert result.outcome is EnumBrokerLagOutcome.ALARM
    assert result.delivery == (
        f"consumer_group_lag:{_GROUP} channel=delivered ledger=delivered",
    )
    [(_, text)] = rig.channel.posts
    assert _GROUP in text
    [(_, row)] = rig.ledger.rows
    assert f"subject={_GROUP}" in row.split(" | ")


def test_the_same_alarm_on_the_next_run_delivers_nothing_and_ok_ends_it() -> None:
    rig = _Rig()
    rig.run(105)
    again = rig.run(110)
    assert again.outcome is EnumBrokerLagOutcome.ALARM
    assert again.delivery == (
        f"consumer_group_lag:{_GROUP} channel=suppressed ledger=not-due",
    )
    assert len(rig.channel.posts) == 1
    assert len(rig.ledger.rows) == 1
    assert rig.run(110).outcome is EnumBrokerLagOutcome.OK
    rig.run(120)
    assert len(rig.channel.posts) == 2
    assert len(rig.ledger.rows) == 1


@pytest.mark.parametrize(
    "growth", [None, {"min_growth": 1, "persist_runs": 1, "streak_path": "s.json"}]
)
def test_every_alarm_line_splits_into_a_declared_group(growth) -> None:  # type: ignore[no-untyped-def]
    rig = _Rig(growth=growth)
    result = rig.run(105)
    typed = typed_alarms(result.alarms, "consumer_group_lag")
    assert [a.subject for a in typed] == [_GROUP]
    assert typed[0].key == f"consumer_group_lag:{_GROUP}"
    assert _GROUP in result.declared_groups


def test_vanished_offsets_split_the_same_way() -> None:
    rig = _Rig()
    rig.lag.never_committed.add(_GROUP)
    result = rig.run(0)
    [alarm] = typed_alarms(result.alarms, "consumer_group_lag")
    assert alarm.subject == _GROUP
    assert "never-committed" in alarm.detail


def test_an_alarm_line_that_changes_shape_is_refused() -> None:
    with pytest.raises(ValueError, match="names no subject"):
        typed_alarms((f"{_GROUP} lag grew 1->2",), "consumer_group_lag")


def test_no_alarm_delivery_block_loads_and_delivers_nothing() -> None:
    rig = _Rig(deliver=False)
    result = rig.run(105)
    assert result.outcome is EnumBrokerLagOutcome.ALARM
    assert result.delivery == ()
    assert rig.channel.posts == [] and rig.ledger.rows == []  # noqa: PT018


def test_alarm_delivery_switched_off_builds_no_delivery() -> None:
    assert _delivery_for(_overlay()) is None
    assert _delivery_for(_overlay(delivery={"enabled": False})) is None


def test_a_failed_delivery_is_named_and_the_run_exits_as_an_alarm() -> None:
    rig = _Rig()
    rig.ledger.refuse = "ledger host unreachable"
    failed = rig.run(105)
    assert failed.outcome is EnumBrokerLagOutcome.ALARM
    assert "delivery-failed" in failed.failure
    assert "ledger host unreachable" in failed.failure
    topics = load_topics()
    assert events_for(failed, topics)[-1][0] == topics["broker_lag_run_failed"]
    # The group stops growing and reads OK, but the row the ledger refused is still owed: the run exits as an alarm.
    out: list[str] = []
    code = run(
        "broker-lag",
        EnumLabAlarmFault.NONE,
        overlay=rig.overlay,
        handler=rig.handler,
        out=out.append,
    )
    assert code == 2
    assert any(line.startswith("BROKER-LAG outcome=OK") for line in out)
    assert any(line.startswith("DELIVERY ") for line in out)
    rig.ledger.refuse = None
    assert (
        run(
            "broker-lag",
            EnumLabAlarmFault.NONE,
            overlay=rig.overlay,
            handler=rig.handler,
            out=out.append,
        )
        == 0
    )
    assert len(rig.ledger.rows) == 1


class _Unreadable:
    def handle(self, request: object) -> None:
        raise OSError("state unreadable")


def test_a_delivery_that_cannot_run_is_named() -> None:
    rig = _Rig()
    rig.handler._delivery = _Unreadable()  # type: ignore[assignment]
    result = rig.run(105)
    assert "delivery-unavailable:OSError" in result.failure


def test_a_probe_and_a_drill_deliver_nothing() -> None:
    rig = _Rig()
    rig.run(105, write=False)
    rig.run(105, fault=EnumLabAlarmFault.PLANTED_LAG)
    assert rig.channel.posts == [] and rig.ledger.rows == []  # noqa: PT018


def test_indeterminate_neither_starts_nor_ends_an_episode() -> None:
    rig = _Rig()
    rig.run(105)
    rig.lag.refused[_GROUP] = "broker down"
    assert rig.run(105).outcome is EnumBrokerLagOutcome.INDETERMINATE
    del rig.lag.refused[_GROUP]
    rig.run(110)
    assert len(rig.channel.posts) == 1
