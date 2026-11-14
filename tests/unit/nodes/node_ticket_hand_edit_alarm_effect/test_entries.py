# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Each routing entry resolves the runtime's typed arm, returns nothing and leaves the event loop free."""

from __future__ import annotations

import asyncio
import inspect
import threading
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_ticket_hand_edit_alarm_effect.handlers.handler_ticket_hand_edit_entries import (
    HandlerTicketHandEditSweep,
    HandlerTicketHandEditWritten,
)
from omnimarket.nodes.node_ticket_hand_edit_alarm_effect.models import (
    ModelTicketContractWritten,
    ModelTicketHandEditSweepTrigger,
)
from omnimarket.validators.routing_input_model_fit import _signature_model_name
from tests.unit.nodes.node_ticket_hand_edit_alarm_effect.support import (
    FINDING_SUFFIX,
    HAND,
    HAND_EDITED_BODY,
    Source,
    issue,
    make_rig,
    revision,
    written,
)

pytestmark = pytest.mark.unit
NODES = Path(__file__).resolve().parents[4] / "src/omnimarket/nodes"
MINE = "node_ticket_hand_edit_alarm_effect"


def _contract() -> dict[str, Any]:
    raw = yaml.safe_load((NODES / MINE / "contract.yaml").read_text())
    assert isinstance(raw, dict)
    return raw


def test_every_routed_handler_resolves_its_event_model() -> None:
    entries = _contract()["handler_routing"]["handlers"]
    assert len(entries) == 2
    for entry in entries:
        resolved = _signature_model_name(
            entry["handler"]["name"], entry["handler"]["module"]
        )
        assert resolved == entry["event_model"]["name"]


def test_the_top_level_union_handler_resolves_no_model() -> None:
    assert (
        _signature_model_name(
            "HandlerTicketHandEditAlarm",
            "omnimarket.nodes.node_ticket_hand_edit_alarm_effect.handlers.handler_ticket_hand_edit_alarm",
        )
        is None
    )


def test_every_entry_is_a_coroutine_returning_nothing() -> None:
    for entry in (HandlerTicketHandEditWritten, HandlerTicketHandEditSweep):
        assert inspect.iscoroutinefunction(entry.handle)
        assert inspect.signature(entry.handle).return_annotation in (None, "None")


def test_the_runtime_fallback_topic_is_consumed_by_no_other_node() -> None:
    fallback = _contract()["event_bus"]["publish_topics"][0]
    assert fallback == "onex.evt.omnimarket.ticket-hand-edit.v1"
    consumers = []
    for path in sorted(NODES.iterdir()):
        contract_path = path / "contract.yaml"
        if path.name == MINE or not contract_path.is_file():
            continue
        text = contract_path.read_text()
        if fallback not in text:
            continue
        other = yaml.safe_load(text)
        bus = other.get("event_bus") or {} if isinstance(other, dict) else {}
        if fallback in (bus.get("subscribe_topics") or []):
            consumers.append(path.name)
    assert consumers == []


def test_the_entries_hand_their_events_to_the_alarm_logic_and_publish_their_own(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = Source([issue(HAND_EDITED_BODY)], [revision(10, HAND)])
    rig = make_rig(monkeypatch, tmp_path, source=source)
    common: dict[str, Any] = {
        "event_publisher": rig.publisher,
        "database": rig.database,
        "source": source,
        "clock": rig.clock,
    }
    record = HandlerTicketHandEditWritten(**common)
    sweep = HandlerTicketHandEditSweep(**common)

    assert (
        asyncio.run(record.handle(ModelTicketContractWritten(**written().model_dump())))
        is None
    )
    assert asyncio.run(sweep.handle(ModelTicketHandEditSweepTrigger())) is None

    assert len(rig.publisher.on(FINDING_SUFFIX)) == 1


def test_a_sweep_waiting_on_linear_leaves_the_event_loop_free(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    release = threading.Event()
    waiting = threading.Event()

    class SlowSource(Source):
        def list_issues_updated_after(self, updated_after: Any) -> Any:
            waiting.set()
            release.wait(timeout=10)
            return super().list_issues_updated_after(updated_after)

    rig = make_rig(monkeypatch, tmp_path, source=SlowSource())
    sweep = HandlerTicketHandEditSweep(
        event_publisher=rig.publisher,
        database=rig.database,
        source=rig.source,
        clock=rig.clock,
    )

    async def scenario() -> int:
        task = asyncio.create_task(sweep.handle(ModelTicketHandEditSweepTrigger()))
        while not waiting.is_set():
            await asyncio.sleep(0.001)
        ticks = 0
        for _ in range(20):  # the loop keeps turning while the read waits
            await asyncio.sleep(0)
            ticks += 1
        release.set()
        await task
        return ticks

    assert asyncio.run(scenario()) == 20
