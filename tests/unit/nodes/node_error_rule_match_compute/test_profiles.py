# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run each declared node adapter on the selected in-memory or Redpanda overlay.

The selected broker proof fails when its binding is unreadable; it never skips.
"""

import asyncio
import importlib
import json
import os
from contextlib import nullcontext
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.event_bus.models.model_event_message import ModelEventMessage

from omnimarket.models.error_rule_match import ModelErrorRuleMatchRequest
from tests.runtime_local_compat import LocalRuntimeBusAdapter
from tests.unit.nodes.node_error_rule_match_compute.test_match import normalize, table
from tests.unit.nodes.node_error_rule_match_compute.test_nomatch import (
    request as exception_request,
)
from tests.unit.nodes.node_error_rule_match_compute.test_replay import corpus_request

ROOT = Path(__file__).parents[4]
DIRECTORY = Path(__file__).parent


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name",
    [
        "node_error_rule_match_compute",
        "node_error_rule_nomatch_effect",
        "node_error_rule_replay_compute",
    ],
)
async def test_nodes_on_selected_overlay(name, tmp_path):
    settings = yaml.safe_load((DIRECTORY / "profiles.yaml").read_text())
    selected = os.environ.get("ONEX_ERROR_RULE_PROFILE", settings["default"])
    profile = settings["profiles"][selected]
    binding = nullcontext()
    if profile["event_bus"] == "kafka":
        from omnibase_infra.cli.delegate_lane import resolve_lane_selection_at
        from omnibase_infra.cli.delegate_lane_credentials import (
            resolve_lane_client_transport_for,
        )
        from omnibase_infra.event_bus.event_bus_kafka import EventBusKafka
        from omnibase_infra.event_bus.lane_client_transport_binding import (
            bind_lane_client_transport,
        )

        from tests.bus_lane_guard import (
            assert_broker_in_declared_lane,
            declared_test_lane,
            resolve_test_bus_broker,
        )

        assert profile["broker_source"] == "declared_test_bus_lane"
        broker = resolve_test_bus_broker()
        assert_broker_in_declared_lane(broker)
        selection = resolve_lane_selection_at(declared_test_lane(), ROOT)
        assert selection.bootstrap_servers == broker
        transport = resolve_lane_client_transport_for(
            lane_target=selection, onex_home=Path.home() / ".onex", environ=os.environ
        )
        binding = (
            bind_lane_client_transport(transport)
            if transport is not None
            else nullcontext()
        )
        with binding:
            bus = EventBusKafka.from_bootstrap(broker)
    else:
        assert profile["event_bus"] == "inmemory"
        bus = EventBusInmemory(environment="error-rule-proof", group="error-rule-proof")
    contract = yaml.safe_load(
        (ROOT / "src/omnimarket/nodes" / name / "contract.yaml").read_text()
    )
    spec = contract["handler"]
    handler = getattr(importlib.import_module(spec["module"]), spec["class"])()
    module, _, model_name = spec["input_model"].rpartition(".")
    input_model = getattr(importlib.import_module(module), model_name)
    suffix = uuid4().hex
    command = (
        contract["runtime_dispatch"]["command_topic"].removesuffix(".v1")
        + f".{suffix}.v1"
    )
    terminal = contract["terminal_event"].removesuffix(".v1") + f".{suffix}.v1"
    observed = asyncio.Queue()
    errors = []
    adapter = LocalRuntimeBusAdapter(
        handler=handler,
        handler_name=spec["class"],
        input_model_cls=input_model,
        output_topic=terminal,
        bus=bus,
        on_error=lambda: errors.append("adapter failed"),
    )

    async def observe(message: ModelEventMessage):
        await observed.put(json.loads(message.value))

    if name == "node_error_rule_match_compute":
        payload = ModelErrorRuleMatchRequest(
            normalized=normalize("uncovered fixture cause"), table=table()
        )
    elif name == "node_error_rule_nomatch_effect":
        payload = exception_request(tmp_path)
    else:
        payload = corpus_request()
        payload = payload.model_copy(update={"records": payload.records[:5]})
    await bus.start()
    try:
        stop_observer = await bus.subscribe_ungrouped(terminal, observe)
        stop_handler = await bus.subscribe_ungrouped(command, adapter.on_message)
        try:
            await bus.publish(
                command, suffix.encode(), payload.model_dump_json().encode()
            )
            result = await asyncio.wait_for(
                observed.get(), timeout=settings["timeout_s"]
            )
            assert not errors
            if name == "node_error_rule_nomatch_effect":
                assert result["queued"]["status"] == "appended"
                assert result["class_key"] == payload.decision.class_key
                rows = (
                    (tmp_path / "queue/exception-queue.jsonl").read_text().splitlines()
                )
                assert len(rows) == 1
            else:
                assert result == handler.handle(payload).model_dump(mode="json")
        finally:
            await stop_handler()
            await stop_observer()
    finally:
        await bus.close()
