# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Plans board reachability through installed discovery and the runtime bus.

The recorded aggregate at 2026-11-03T21:42:17Z was 119 plans, 64 stalled,
54 without an owner. Only those counts were supplied, not the source snapshot.
The source world below is a reconstruction with those counts and explicit
controls; it makes no claim about the original plans' individual causes.
"""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts
from omnibase_infra.runtime.auto_wiring.handler_wiring import wire_from_manifest
from omnibase_infra.runtime.auto_wiring.models import ModelAutoWiringManifest
from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine

from omnimarket.nodes.node_plans_today_effect import node
from omnimarket.nodes.node_plans_today_effect.handlers import handler_plans_today
from omnimarket.nodes.node_plans_today_effect.models.model_plans_today import (
    ModelPlansTodayRequest,
    ModelPlansTodayResult,
)
from tests.nodes.node_plans_today_effect.conftest import (
    body,
    make_fixture,
    pr,
    row,
)

pytestmark = pytest.mark.unit

RECORDED_AT = "2026-11-03T21:42:17Z"
OLD = "2026-11-01T20:00:00Z"


def recorded_count_world(tmp_path, monkeypatch):
    plans, prs, rows = {}, {}, []
    for index in range(119):
        ticket = f"OMN-{1000 + index}"
        plans[f"2026-11-03-plan-{index:03}.md"] = body(
            f"Plan {index}", [("work", ticket)]
        )
        prs[f"example-repo#{index + 1}"] = pr(
            ticket,
            cls="red" if index < 64 else "green-unarmed",
            state="OPEN" if index < 64 else "MERGED",
            updated=OLD,
        )
        if 54 <= index < 64:
            lane = f"fixture-owner-{index}"
            rows += [
                row(len(rows) + 1, OLD, "CLAIM", lane, ticket),
                # Liveness of the owner is independent of movement on this plan.
                row(len(rows) + 2, RECORDED_AT, "STATUS", lane, "OMN-9999"),
            ]
    fx = make_fixture(tmp_path, monkeypatch, plans, prs=prs, rows=rows)
    watcher = json.loads(fx["watcher"].read_text())
    watcher["last_tick"] = RECORDED_AT
    fx["watcher"].write_text(json.dumps(watcher))
    sent = []

    def emit(plan, to, lane, home, plugin_root):
        sent.append((plan["path"], to, lane))
        return True, "OK MSG fixture"

    monkeypatch.setattr(handler_plans_today, "emit_msg", emit)
    request = ModelPlansTodayRequest(
        home=str(tmp_path),
        kb=str(fx["kb"]),
        no_fetch=True,
        watcher=str(fx["watcher"]),
        linear_snapshot=str(fx["linear"]),
        ledger_rows_file=str(fx["ledger"]),
        summaries="none",
        out_dir=str(fx["out"]),
        now=RECORDED_AT,
        orchestrator="fixture-orchestrator",
    )
    return fx, request, sent


async def test_recorded_counts_reach_plans_handler_through_runtime_bus(
    tmp_path, monkeypatch
):
    contracts = tuple(
        contract
        for contract in discover_contracts().contracts
        if contract.name == "node_plans_today_effect"
    )
    assert len(contracts) == 1
    raw = yaml.safe_load(Path(node.__file__).with_name("contract.yaml").read_text())
    assert raw["event_bus"]["subscribe_topics"], (
        "the plans board has no command subscription: a runtime tick cannot reach it"
    )
    fx, request, sent = recorded_count_world(tmp_path, monkeypatch)
    bus = EventBusInmemory(environment="local", group="plans-board")
    await bus.start()
    completed = asyncio.Queue()
    completion_topic = "onex.evt.omnimarket.plans-today-completed.v1"
    assert raw["terminal_event"] == completion_topic

    async def collect(message):
        assert message.topic == completion_topic
        await completed.put(json.loads(message.value))

    try:
        await bus.subscribe(
            completion_topic, group_id="plans-board-results", on_message=collect
        )
        engine = MessageDispatchEngine()
        report = await wire_from_manifest(
            ModelAutoWiringManifest(contracts=contracts),
            engine,
            event_bus=bus,
            environment="local",
        )
        assert [result.outcome.value for result in report.results] == ["wired"]
        assert set(report.results[0].topics_subscribed) == set(
            raw["event_bus"]["subscribe_topics"]
        )
        engine.freeze()
        correlation = uuid4()
        envelope = ModelEventEnvelope[object](
            payload=request.model_dump(mode="json"),
            correlation_id=correlation,
            event_type="omnimarket.plans-today-requested",
        )
        await bus.publish(
            raw["runtime_dispatch"]["command_topic"],
            None,
            envelope.model_dump_json().encode(),
            None,
        )
        event = await asyncio.wait_for(completed.get(), timeout=10)
        assert event["correlation_id"] == str(correlation)
        result = ModelPlansTodayResult.model_validate(event["payload"])
        assert result.exit_code == 0, result.refused
        assert result.mode == "operator"
        document = result.document
        assert document is not None
        assert len(document["plans"]) == 119
        assert Counter(plan["verdict"] for plan in document["plans"]) == {
            "STALLED": 64,
            "DONE": 55,
        }
        unowned = {
            plan["path"]
            for plan in document["plans"]
            if plan["verdict"] == "STALLED"
            and not handler_plans_today.is_owned(plan["owner"])
        }
        assert len(unowned) == 54
        assert {path for path, _, _ in sent} == unowned
        assert len(sent) == 54
        assert all(to == "fixture-orchestrator" for _, to, _ in sent)
        assert len(document["msgs"]) == 54
        assert all(message["action"] == "sent" for message in document["msgs"])
        assert json.loads((fx["out"] / "plans-today.json").read_text()) == document

        # An unchanged second tick reaches the same handler and emits no duplicates.
        await bus.publish(
            raw["runtime_dispatch"]["command_topic"],
            None,
            envelope.model_dump_json().encode(),
            None,
        )
        second = await asyncio.wait_for(completed.get(), timeout=10)
        repeated = ModelPlansTodayResult.model_validate(second["payload"])
        assert repeated.exit_code == 0
        assert repeated.document is not None
        assert len(sent) == 54
        assert all(m["action"] == "skipped" for m in repeated.document["msgs"])

        # A caller without its identity sidecar receives a correlated refusal;
        # bus dispatch must not grant the operator's view or emit more notices.
        refused_request = request.model_copy(
            update={"linear_snapshot": str(tmp_path / "unread.txt")}
        )
        refused_envelope = ModelEventEnvelope[object](
            payload=refused_request.model_dump(mode="json"),
            correlation_id=uuid4(),
            event_type="omnimarket.plans-today-requested",
        )
        await bus.publish(
            raw["runtime_dispatch"]["command_topic"],
            None,
            refused_envelope.model_dump_json().encode(),
            None,
        )
        refused_event = await asyncio.wait_for(completed.get(), timeout=10)
        refused = ModelPlansTodayResult.model_validate(refused_event["payload"])
        assert refused_event["correlation_id"] == str(refused_envelope.correlation_id)
        assert refused.exit_code == 2
        assert refused.mode is None
        assert refused.refused
        assert refused.document is None
        assert len(sent) == 54
    finally:
        await bus.close()
