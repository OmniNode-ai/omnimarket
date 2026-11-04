# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Same synthetic one-day Stop records through production dispatch and either bus overlay.

The lab selects the Redpanda leg with ONEX_PROMISE_PROOF_OVERLAY pointing at
fixtures/overlays/redpanda.yaml. Without it, the in-memory leg runs everywhere.
No transport absence is a skip or a pass. Proof topics are isolated per run.
"""

import asyncio
import hashlib
import json
import os
from importlib.resources import files
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts
from omnibase_infra.runtime.auto_wiring.handler_wiring import wire_from_manifest
from omnibase_infra.runtime.auto_wiring.models import ModelAutoWiringManifest
from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine

from omnimarket.delegated_test_loop.lane_bus import open_lab_run_bus
from omnimarket.handlers.bus_binding.node_event_bus_overlay import read_node_event_bus
from omnimarket.handlers.bus_binding.select import positional_opener
from omnimarket.models.model_orchestrator_promise import (
    ModelPromiseCaptureRecord,
    ModelPromiseEffectOverlay,
    ModelPromisePolicy,
)
from omnimarket.nodes.node_orchestrator_promise_capture_effect.container.container_promise_capture import (
    build_handler,
)
from omnimarket.nodes.node_orchestrator_promise_capture_effect.handlers.store_promise_capture import (
    StorePromiseCapture,
)
from omnimarket.nodes.node_orchestrator_promise_classify_compute.node import (
    NodeOrchestratorPromiseClassifyCompute,
)

pytestmark = pytest.mark.unit
FIXTURES = Path(__file__).with_name("fixtures")


async def test_promise_classify_overlay_day_replay_matches_pure_output(
    tmp_path,
) -> None:
    selected = (
        Path(os.environ["ONEX_PROMISE_PROOF_OVERLAY"])
        if "ONEX_PROMISE_PROOF_OVERLAY" in os.environ
        else FIXTURES / "overlays" / "inmemory.yaml"
    )
    binding = read_node_event_bus(selected)
    assert binding is not None
    policy = ModelPromisePolicy.model_validate(
        yaml.safe_load(
            files("omnimarket.nodes.node_orchestrator_promise_classify_compute")
            .joinpath("contract.yaml")
            .read_text()
        )["config"]
    )
    items = json.loads((FIXTURES / "labelled_regressions.json").read_text())["samples"]
    records = tuple(
        ModelPromiseCaptureRecord(
            session_id="synthetic-day",
            lane="orchestrator-fixture",
            content_kind="assistant_reply",
            content_record_id=item["id"],
            emitted_at=f"2026-11-04T12:{index:02}:00Z",
            content=item["text"],
        )
        for index, item in enumerate(items)
    )
    pure = build_handler(store_path=":memory:", policy=policy)
    expected = [pure.handle(record).model_dump(mode="json") for record in records]
    store = StorePromiseCapture(tmp_path / "transport.sqlite")
    overlay = ModelPromiseEffectOverlay(
        store_path=str(tmp_path / "transport.sqlite"),
        assistant_reply_kind="assistant_reply",
        policy=policy,
    )
    node_name = "node_orchestrator_promise_capture_effect"
    discovered = next(c for c in discover_contracts().contracts if c.name == node_name)
    unique = uuid4().hex
    # Override only the two bus topics at the composition boundary. Shared lab consumers
    # never see synthetic records on the real Stop topic.
    proof_topics = yaml.safe_load(selected.read_text())["proof_topics"]
    command = proof_topics["command"].format(run_id=unique)
    terminal = proof_topics["terminal"].format(run_id=unique)
    routing = discovered.model_dump()["handler_routing"]
    routing["handlers"] = [dict(route, topic=command) for route in routing["handlers"]]
    isolated = type(discovered).model_validate(
        {
            **discovered.model_dump(),
            "event_bus": {"subscribe_topics": [command], "publish_topics": [terminal]},
            "handler_routing": routing,
            "terminal_event": terminal,
        }
    )
    received = asyncio.Queue()

    async def collect(message):
        await received.put(json.loads(message.value))

    async with positional_opener(open_lab_run_bus)(
        binding.type,
        binding.lane,
        None,
        Path(os.environ["OMNI_HOME"]) if "OMNI_HOME" in os.environ else None,
    ) as bus:
        await bus.subscribe(
            terminal, group_id=f"promise-proof-{unique}", on_message=collect
        )
        engine = MessageDispatchEngine()
        report = await wire_from_manifest(
            ModelAutoWiringManifest(contracts=(isolated,)),
            engine,
            event_bus=bus,
            environment=f"proof-{unique}",
            materialized_explicit_dependencies={
                "HandlerPromiseCapture": {
                    "store": store,
                    "classifier": NodeOrchestratorPromiseClassifyCompute(),
                    "overlay": overlay,
                },
            },
        )
        assert [item.outcome.value for item in report.results] == ["wired"]
        engine.freeze()
        actual = []
        for record in records:
            envelope = ModelEventEnvelope[object](
                payload=record.model_dump(mode="json"), event_type=command
            )
            await bus.publish(command, None, envelope.model_dump_json().encode(), None)
            result = await asyncio.wait_for(received.get(), timeout=30)
            actual.append(result["payload"])
        assert actual == expected
        for record in records:
            envelope = ModelEventEnvelope[object](
                payload=record.model_dump(mode="json"), event_type=command
            )
            await bus.publish(command, None, envelope.model_dump_json().encode(), None)
            replay = (await asyncio.wait_for(received.get(), timeout=30))["payload"]
            assert replay["status"] == "duplicate"
            assert replay["rows"] == replay["ledger_rows"] == []
            assert replay["candidate_count"] == 40
        digest = hashlib.sha256(json.dumps(actual, sort_keys=True).encode()).hexdigest()
        print(
            f"promise overlay={binding.type} synthetic_stop_records={len(records)} replay_new_rows=0 candidate_count=40 output_sha256={digest}"
        )
    store.close()
