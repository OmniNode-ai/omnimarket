# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ledger ANDONs enter the queue through its detector and remain readback gated."""

from __future__ import annotations

import importlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.handlers.rules_stop_gate import load_andon_stop_contract, stop_gate
from omnimarket.models.model_andon_ledger_holds import ModelAndonLedgerHoldsRequest
from omnimarket.models.stop_gate import (
    EnumReadingOrigin,
    EnumReadingVerdict,
    EnumStopEventKind,
    EnumStopGateOutcome,
    EnumStopMode,
    ModelStopEvent,
    ModelStopGateRequest,
    ModelStopPolicy,
    ModelStopReading,
)
from omnimarket.nodes.node_exception_queue_effect.container.container_exception_queue_effect import (
    create_handler_open_stops,
)
from omnimarket.nodes.node_exception_queue_effect.handler import HandlerStopEvent
from omnimarket.nodes.node_exception_queue_effect.models import (
    ModelOpenStopsRequest,
    ModelStopEventRequest,
)
from omnimarket.nodes.node_exception_queue_effect.node import NodeExceptionQueueEffect

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 2, 3, 0, tzinfo=UTC)
HOLDS = (
    "2026-11-02T02:31:20Z | HOLD | lane=operator-fixture | "
    "id=2026-11-02T02:31:20Z-operator-fixture | to=all | until=2026-11-02T14:00:00Z | scope=dispatch | "
    "class=seam-mismatch | key=prepr-slot | swarm=prove-5554-preslot-1101 | "
    "readback=prepr_verify_lane.sh reads ready twice | ANDON stop",
    "2026-11-02T02:31:37Z | HOLD | lane=operator-fixture | "
    "id=2026-11-02T02:31:37Z-operator-fixture | to=all | until=2026-11-02T14:00:00Z | scope=dispatch | "
    "class=seam-mismatch | key=dev-runtime-ready | swarm=dev-deploy-merged-5554 | "
    "readback=dev ready reads 200 twice | ANDON stop",
)


def test_andon_hold_opens_stop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ledger = tmp_path / "ledger.md"
    ledger.write_text("\n".join(HOLDS) + "\n", encoding="utf-8")
    monkeypatch.setenv("ONEX_LEDGER_PATH", str(ledger))
    result = create_handler_open_stops().handle(
        ModelOpenStopsRequest(tmp_path / "queue", now=NOW)
    )
    assert {(s.cls, s.scope.key) for s in result.stops} == {
        ("seam-mismatch", "prepr-slot"),
        ("seam-mismatch", "dev-runtime-ready"),
    }
    assert {s.swarm_lane for s in result.stops} == {
        "prove-5554-preslot-1101",
        "dev-deploy-merged-5554",
    }
    assert all(s.state == "swarming" and s.lease_expires_at for s in result.stops)
    assert result.reports == ()
    assert result.refusals == ()
    for stop in result.stops:
        decision = stop_gate(
            ModelStopGateRequest(
                scope=stop.scope,
                stops=result.stops,
                policy=ModelStopPolicy(modes={stop.cls: EnumStopMode.ENFORCE}),
                now=NOW,
            ),
            load_andon_stop_contract()[0],
        )
        assert decision.outcome is EnumStopGateOutcome.REFUSE
        assert decision.stop_id == stop.id
        assert decision.swarm_lane == stop.swarm_lane
    queue = tmp_path / "queue" / "exception-queue.jsonl"
    before = queue.read_bytes()
    again = create_handler_open_stops().handle(
        ModelOpenStopsRequest(tmp_path / "queue", now=NOW + timedelta(minutes=10))
    )
    assert again.stops == result.stops
    assert queue.read_bytes() == before


def test_andon_release_keeps_readback(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.md"
    ledger.write_text("\n".join(HOLDS) + "\n", encoding="utf-8")
    request = ModelOpenStopsRequest(tmp_path / "queue", ledger, NOW)
    node = NodeExceptionQueueEffect()
    before = node.handle(request)
    assert len(before.stops) == 2
    with ledger.open("a", encoding="utf-8") as stream:
        for stamp in ("20", "37"):
            stream.write(
                "2026-11-02T03:00:00Z | RELEASE | lane=operator-fixture | "
                f"re=2026-11-02T02:31:{stamp}Z-operator-fixture | cause repaired\n"
            )
    after = node.handle(request)
    assert after.stops == before.stops
    assert {s.readback_command for s in after.stops} == {
        "prepr_verify_lane.sh reads ready twice",
        "dev ready reads 200 twice",
    }
    assert after.refusals == ()


def test_andon_unregistered_class_reported(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.md"
    ledger.write_text(
        HOLDS[0].replace("class=seam-mismatch", "class=unknown-fixture") + "\n",
        encoding="utf-8",
    )
    result = NodeExceptionQueueEffect().handle(
        ModelOpenStopsRequest(tmp_path / "queue", ledger, NOW)
    )
    assert result.stops == ()
    assert result.reports == (
        "hold=2026-11-02T02:31:20Z-operator-fixture class_not_registered:unknown-fixture",
    )
    assert not (tmp_path / "queue" / "exception-queue.jsonl").exists()


@pytest.mark.parametrize("case", ["released", "expired", "future", "plain"])
def test_andon_hold_only_ingests_in_force_rows(tmp_path: Path, case: str) -> None:
    text = HOLDS[0]
    if case == "released":
        text += "\n2026-11-02T02:59:00Z | RELEASE | lane=operator-fixture | re=2026-11-02T02:31:20Z-operator-fixture"
    elif case == "expired":
        text = text.replace("until=2026-11-02T14:00:00Z", "until=2026-11-02T03:00:00Z")
    elif case == "future":
        text = text.replace("2026-11-02T02:31:20Z", "2026-11-03T02:31:20Z")
    else:
        text = text.replace("class=seam-mismatch | ", "")
    ledger = tmp_path / "ledger.md"
    ledger.write_text(text + "\n", encoding="utf-8")
    result = NodeExceptionQueueEffect().handle(
        ModelOpenStopsRequest(tmp_path / "queue", ledger, NOW)
    )
    assert result.stops == ()
    assert result.reports == ()


def _event(
    queue: Path, stop_id: str, kind: EnumStopEventKind, minute: int, **extra: Any
) -> None:
    role = "readback" if kind is EnumStopEventKind.READBACK else "swarm_lane"
    result = HandlerStopEvent().handle(
        ModelStopEventRequest(
            queue,
            ModelStopEvent(
                stop_id=stop_id,
                kind=kind,
                role=role,
                at=NOW + timedelta(minutes=minute),
                **extra,
            ),
            (NOW + timedelta(minutes=minute)).isoformat(),
        )
    )
    assert result.status == "applied", result.reason


def test_andon_hold_readback_clears_once_and_replay_never_reopens(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "ledger.md"
    # Two distinct HOLD ids for the same case must both remain consumed after closure.
    second = HOLDS[0].replace("02:31:20Z", "02:31:21Z")
    ledger.write_text(HOLDS[0] + "\n" + second + "\n", encoding="utf-8")
    request = ModelOpenStopsRequest(tmp_path / "queue", ledger, NOW)
    node = NodeExceptionQueueEffect()
    [stop] = node.handle(request).stops
    _event(request.state_dir, stop.id, EnumStopEventKind.COUNTERMEASURE_MERGED, 1)
    fixed = ModelStopReading(
        verdict=EnumReadingVerdict.PASS,
        proof_eligible=True,
        origin=EnumReadingOrigin.EXECUTED,
        covers_scope=True,
    )
    _event(request.state_dir, stop.id, EnumStopEventKind.READBACK, 2, reading=fixed)
    assert len(node.handle(request).stops) == 1
    _event(request.state_dir, stop.id, EnumStopEventKind.READBACK, 3, reading=fixed)
    queue = request.state_dir / "exception-queue.jsonl"
    before = queue.read_bytes()
    assert NodeExceptionQueueEffect().handle(request).stops == ()
    assert queue.read_bytes() == before
    with ledger.open("a", encoding="utf-8") as stream:
        stream.write(HOLDS[0].replace("02:31:20Z", "02:31:22Z") + "\n")
    [recurrence] = NodeExceptionQueueEffect().handle(request).stops
    assert recurrence.id != stop.id


def test_andon_hold_retains_free_text_readback(tmp_path: Path) -> None:
    readback = "slot ready reads 200 twice. An unreadable result keeps this stop."
    ledger = tmp_path / "ledger.md"
    ledger.write_text(
        HOLDS[0].split("readback=", 1)[0] + "ANDON stop.\nReadback: " + readback + "\n",
        encoding="utf-8",
    )
    [stop] = (
        NodeExceptionQueueEffect()
        .handle(ModelOpenStopsRequest(tmp_path / "queue", ledger, NOW))
        .stops
    )
    assert stop.readback_command == readback


@pytest.mark.asyncio
async def test_andon_hold_deploy_bus_route(tmp_path: Path) -> None:
    """The shipped contract's declared detector runs through the runtime bus adapter."""
    from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

    from tests.runtime_local_compat import LocalRuntimeBusAdapter

    contract = yaml.safe_load(NodeExceptionQueueEffect.contract_path.read_text())
    route = next(
        r
        for r in contract["handler_routing"]["handlers"]
        if r["operation"] == "sync_ledger_holds"
    )
    cls = getattr(
        importlib.import_module(route["handler"]["module"]), route["handler"]["name"]
    )
    model = getattr(
        importlib.import_module(route["event_model"]["module"]),
        route["event_model"]["name"],
    )
    assert model is ModelAndonLedgerHoldsRequest
    assert route["topic"] in contract["event_bus"]["subscribe_topics"]
    ledger = tmp_path / "ledger.md"
    ledger.write_text("\n".join(HOLDS) + "\n", encoding="utf-8")
    bus = EventBusInmemory()
    adapter = LocalRuntimeBusAdapter(
        handler=cls(),
        handler_name=route["handler"]["name"],
        input_model_cls=model,
        output_topic=contract["terminal_event"],
        bus=bus,
    )
    await bus.start()
    try:
        await bus.subscribe(
            route["topic"], on_message=adapter.on_message, group_id="andon-hold-fixture"
        )
        await bus.publish(
            route["topic"],
            None,
            model(state_dir=tmp_path / "queue", ledger_path=ledger, now=NOW)
            .model_dump_json()
            .encode(),
        )
        history = await bus.get_event_history(topic=contract["terminal_event"])
        assert len(history) == 1
        result = json.loads(history[0].value)
        assert len(result["stop_ids"]) == 2
        assert result["reports"] == []
        opened = NodeExceptionQueueEffect().handle(
            ModelOpenStopsRequest(tmp_path / "queue", ledger, NOW)
        )
        assert {s.scope.key for s in opened.stops} == {
            "prepr-slot",
            "dev-runtime-ready",
        }
    finally:
        await bus.close()
