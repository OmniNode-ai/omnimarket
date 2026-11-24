# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The held-command age bound is a settings-store value the gate follows without a restart (OMN-26484).

``held_age_alarm_after_seconds`` is set through ``node_overlay_setting_set_effect`` (a declared key of the gate's
overlay), the store's refresh puts the record in the host's snapshot root, and the gate reads its policy from
``<root>/node_dev_roll_window_gate_effect/overlay.yaml`` under ``ONEX_SKILL_OVERLAY_ROOTS`` on every tick: a
changed stored value moves the alarm on the next tick, and an unset value switches the alarm off, with the same
handler objects and no restart. The tested default is the value ``overlay.example.yaml`` carries.
"""

from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import Mapping, Sequence
from datetime import timedelta
from importlib import resources
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick
from pydantic import BaseModel, JsonValue

from omnimarket.events.runtime_deployment import ModelDeployPublishCommand
from omnimarket.handlers.overlay_store.snapshot_root import render_document
from omnimarket.models.lab_alarm_delivery import ModelLabAlarmDeliveryRequest
from omnimarket.models.overlay_store import ModelOverlayStoreRecord, document_digest
from omnimarket.nodes.node_dev_roll_window_gate_effect.handlers import (
    HandlerDevRollWindowHold,
    HandlerDevRollWindowTick,
    InMemoryDevRollGateStore,
)
from omnimarket.nodes.node_dev_roll_window_gate_effect.models import (
    ModelDevRollWindowGatePolicy,
)
from omnimarket.nodes.node_dev_roll_window_gate_effect.overlay import NODE_NAME
from omnimarket.nodes.node_overlay_setting_set_effect.handlers.handler_overlay_setting_set import (
    HandlerOverlaySettingSet,
    SettingNotDeclaredError,
    load_settable_settings,
)
from omnimarket.nodes.node_overlay_setting_set_effect.models.model_overlay_setting_set_request import (
    ModelOverlaySettingSetRequest,
)
from tests.unit.nodes.node_dev_roll_window_gate_effect.conftest import (
    NOW,
    OVERLAY_DOC,
    WINDOW,
    Clock,
    command,
)
from tests.unit.nodes.node_overlay_setting_set.support import (
    FakeBus,
    FakeLive,
    contract,
)

pytestmark = pytest.mark.unit

KEY = "held_age_alarm_after_seconds"
ACTOR = "heldalarm-0040"
CONDITION = "dev_roll_held_command_age"
# A roll no completion has reached, held behind for hours: only the age bound decides the alarm.
LONG = 6 * 3600
STORED_DOC: dict[str, Any] = {k: v for k, v in OVERLAY_DOC.items() if k != KEY} | {
    "failed_roll_release_after_seconds": LONG,
    "roll_completion_deadline_seconds": LONG,
}


class StoreAndGate:
    """The store's live record of the gate overlay, the setter over it, the snapshot root, and one gate that is never rebuilt."""

    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path / "store-root"
        self.clock = Clock()
        self.bus = FakeBus()
        record = ModelOverlayStoreRecord(
            overlay_id=NODE_NAME,
            digest=document_digest(dict(STORED_DOC)),
            document=dict(STORED_DOC),
            source_ref="node_dev_roll_window_gate_effect@abc123",
            loaded_by="loader",
            loaded_at=NOW - timedelta(minutes=20),
        ).model_dump(mode="json")
        self.live = FakeLive({NODE_NAME: record})
        spec = contract()
        topics = spec["event_bus"]["publish_topics"]
        self.setter = HandlerOverlaySettingSet(
            reader=self.live,
            bus=self.bus,
            settings=load_settable_settings(spec),
            loaded_topic=topics[0],
            audit_topic=topics[1],
            max_live_age_s=7200.0,
            clock=lambda: NOW,
        )
        self.environ = {"ONEX_SKILL_OVERLAY_ROOTS": str(self.root)}
        self.refresh(STORED_DOC)  # the store holds the document with the key absent
        self.store = InMemoryDevRollGateStore()
        # Built with no policy: each handler reads it from the snapshot root, the way the runtime builds them.
        self.hold = HandlerDevRollWindowHold(
            self.store, environ=self.environ, clock=self.clock
        )
        self.tick = HandlerDevRollWindowTick(
            self.store, environ=self.environ, clock=self.clock
        )

    def refresh(self, document: Mapping[str, JsonValue]) -> None:
        """What the host's refresh does with the store's version: the snapshot file under the root."""
        path = self.root / NODE_NAME / "overlay.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(render_document(document))

    def store_set(self, value: JsonValue) -> None:
        """Set the declared key through the settings store, then let the host's snapshot take the stored version."""
        asyncio.run(
            self.setter.handle(
                ModelOverlaySettingSetRequest(
                    overlay_id=NODE_NAME, key=KEY, value=value, actor=ACTOR
                )
            )
        )
        loaded_topic = contract()["event_bus"]["publish_topics"][0]
        (_, published) = self.bus.on(loaded_topic)[-1]
        self.refresh(json.loads(published)["document"])

    def a_tick(self) -> ModelRuntimeTick:
        return ModelRuntimeTick(
            now=self.clock.now,
            tick_id=uuid4(),
            sequence_number=1,
            scheduled_at=self.clock.now,
            correlation_id=uuid4(),
            scheduler_id="test",
            tick_interval_ms=1000,
        )

    def alarms(self) -> list[ModelLabAlarmDeliveryRequest]:
        out: Sequence[BaseModel] | None = self.tick.handle(self.a_tick())
        return [m for m in out or [] if isinstance(m, ModelLabAlarmDeliveryRequest)]

    def roll(self) -> str:
        """Hold three commands, let the window pass and release them: the roll the later commands wait behind."""
        for number in (1, 2, 3):
            self.hold.handle(command(number=number, age_seconds=WINDOW))
        self.clock.advance(WINDOW)
        out = self.tick.handle(self.a_tick())
        sent = [m for m in out or [] if isinstance(m, ModelDeployPublishCommand)]
        assert len(sent) == 3
        return str(sent[-1].correlation_id)


@pytest.fixture
def world(tmp_path: Path) -> StoreAndGate:
    return StoreAndGate(tmp_path)


def test_roll_gate_held_age_alarm_store_value_alarms_a_command_held_over_1800_seconds(
    world: StoreAndGate,
) -> None:
    """The held fixture: a command held 1801 s behind a roll raises one alarm naming both, once the store holds 1800."""
    job_id = world.roll()
    world.store_set(1800)
    held = command(number=8, requested_by="gha/omnimarket/pr-6586")
    world.hold.handle(held)
    world.clock.advance(1800)  # exactly as old as the bound: not older
    assert world.alarms() == []
    world.clock.advance(1)
    (request,) = world.alarms()
    assert request.condition == CONDITION
    (alarm,) = request.alarms
    for text in (alarm.subject, alarm.detail):
        assert job_id in text
        assert str(held.correlation_id) in text
    assert world.store.load().alarmed_ids == (str(held.correlation_id),)


def test_roll_gate_held_age_alarm_is_off_while_the_store_holds_no_value(
    world: StoreAndGate,
) -> None:
    """The control: with the stored value absent the same aged command raises nothing and nothing is alarmed."""
    world.roll()
    world.hold.handle(command(number=8))
    world.clock.advance(5000)
    assert world.alarms() == []
    assert world.store.load().alarmed_ids == ()


def test_roll_gate_held_age_alarm_follows_the_stored_value_without_a_restart(
    world: StoreAndGate,
) -> None:
    """One gate, never rebuilt: absent -> 1800 -> 600 -> unset each move the alarm on the next tick."""
    job_id = world.roll()
    first = command(number=8)
    world.hold.handle(first)
    world.clock.advance(1900)
    assert world.alarms() == []  # the stored value is absent: the alarm is off
    world.store_set(1800)
    (one,) = world.alarms()  # the same held command is now older than the stored 1800
    assert str(first.correlation_id) in one.alarms[0].detail
    assert world.store.load().alarmed_ids == (str(first.correlation_id),)

    second = command(number=9)
    world.hold.handle(second)
    world.clock.advance(601)
    assert world.alarms() == []  # 601 s old: under the stored 1800
    world.store_set(600)
    (two,) = (
        world.alarms()
    )  # the stored 600 is read on the next tick: the second command is over it
    assert job_id in two.alarms[0].detail
    assert str(second.correlation_id) in two.alarms[0].detail
    assert world.store.load().alarmed_ids == (
        str(first.correlation_id),
        str(second.correlation_id),
    )

    third = command(number=10)
    world.hold.handle(third)
    world.clock.advance(5000)
    world.store_set(None)  # unset: the policy field is absent again
    assert world.alarms() == []
    assert str(third.correlation_id) not in world.store.load().alarmed_ids


def test_roll_gate_held_age_alarm_setting_is_the_only_gate_key_the_store_declares() -> (
    None
):
    """The held-age key is declared settable for the gate's overlay; another key of that overlay is refused."""
    settings = load_settable_settings(contract())
    declared = {s.key for s in settings if s.overlay_id == NODE_NAME}
    assert declared == {KEY}
    handler = HandlerOverlaySettingSet(
        reader=FakeLive({}),
        bus=FakeBus(),
        settings=settings,
        loaded_topic="loaded",
        audit_topic="audit",
        max_live_age_s=1.0,
    )
    with pytest.raises(SettingNotDeclaredError):
        asyncio.run(
            handler.handle(
                ModelOverlaySettingSetRequest(
                    overlay_id=NODE_NAME, key="mode", value="off", actor=ACTOR
                )
            )
        )


def example_problems(document: Mapping[str, Any]) -> list[str]:
    """What is wrong with the gate overlay's tested default."""
    found: list[str] = []
    if document.get(KEY) != 1800:
        found.append(f"{KEY} is not 1800")
    try:
        ModelDevRollWindowGatePolicy.model_validate(dict(document))
    except ValueError:
        found.append("the document is not a gate policy")
    return found


def example_document() -> dict[str, Any]:
    text = (
        resources.files(f"omnimarket.nodes.{NODE_NAME}")
        .joinpath("overlay.example.yaml")
        .read_text(encoding="utf-8")
    )
    loaded: dict[str, Any] = yaml.safe_load(text)
    return loaded


def test_roll_gate_held_age_alarm_tested_default_is_1800_in_the_overlay_example() -> (
    None
):
    assert example_problems(example_document()) == []


def test_roll_gate_held_age_alarm_control_a_dropped_key_is_found() -> None:
    """The positive control: the check above fails when the key is removed from the overlay document."""
    document = copy.deepcopy(example_document())
    del document[KEY]
    assert f"{KEY} is not 1800" in example_problems(document)
