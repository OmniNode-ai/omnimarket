# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The canonical runtime adapter replays every frozen row on a selected bus.

Default in-memory; select redpanda using ONEX_ERROR_NORMALIZER_PROFILE. Missing
credentials or a broker failure fail the selected proof, never skip it.
"""

from __future__ import annotations

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

from omnimarket.models.error_normalizer import (
    ModelErrorClassNormalizeRequest,
    ModelErrorClassNormalizeResult,
    ModelErrorEvent,
    ModelErrorNormalizerFacts,
)
from tests.runtime_local_compat import LocalRuntimeBusAdapter

DIRECTORY = Path(__file__).parent
NODE = (
    Path(__file__).parents[4]
    / "src/omnimarket/nodes/node_error_class_normalize_compute"
)


@pytest.mark.asyncio
async def test_corpus_keys_under_selected_bus_profile() -> None:
    profiles = yaml.safe_load((DIRECTORY / "profiles.yaml").read_text())
    selected = os.environ.get("ONEX_ERROR_NORMALIZER_PROFILE", profiles["default"])
    profile = profiles["profiles"][selected]
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
        selection = resolve_lane_selection_at(
            declared_test_lane(), Path(__file__).parents[4]
        )
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
        bus = EventBusInmemory(
            environment="normalizer-replay", group="normalizer-replay"
        )
    contract = yaml.safe_load((NODE / "contract.yaml").read_text())
    assert contract["terminal_event"] == "onex.evt.omnimarket.error-class-normalized.v1"
    assert contract["event_bus"]["publish_topics"] == [contract["terminal_event"]]
    spec = contract["handler"]
    handler = getattr(importlib.import_module(spec["module"]), spec["class"])()
    # Fresh suffixes isolate this proof from runtime consumers and retained data.
    suffix = uuid4().hex
    command = (
        contract["runtime_dispatch"]["command_topic"].removesuffix(".v1")
        + f".{suffix}.v1"
    )
    terminal = contract["terminal_event"].removesuffix(".v1") + f".{suffix}.v1"
    results: asyncio.Queue[ModelErrorClassNormalizeResult] = asyncio.Queue()
    errors = []
    adapter = LocalRuntimeBusAdapter(
        handler=handler,
        handler_name=spec["class"],
        input_model_cls=ModelErrorClassNormalizeRequest,
        output_topic=terminal,
        bus=bus,
        on_error=lambda: errors.append("adapter failed"),
    )

    async def observe(message: ModelEventMessage) -> None:
        assert message.topic == terminal
        await results.put(
            ModelErrorClassNormalizeResult.model_validate_json(message.value)
        )

    fixtures = DIRECTORY / "fixtures"
    manifest = json.loads((fixtures / "manifest.json").read_text())
    facts = ModelErrorNormalizerFacts.model_validate_json(
        (fixtures / "normalizer_facts.json").read_text()
    )
    actual, expected = [], []
    await bus.start()
    try:
        stop_observe = await bus.subscribe_ungrouped(terminal, observe)
        stop_handler = await bus.subscribe_ungrouped(command, adapter.on_message)
        try:
            for item in manifest["corpus_files"]:
                for line in (fixtures / item["path"]).read_text().splitlines():
                    row = json.loads(line)
                    request = ModelErrorClassNormalizeRequest(
                        event=ModelErrorEvent.model_validate(row["event"]),
                        facts=facts,
                        key_version=manifest["baseline"]["new_normalizer"][
                            "key_version"
                        ],
                    )
                    direct = handler.handle(request)
                    await bus.publish(
                        command, row["id"].encode(), request.model_dump_json().encode()
                    )
                    result = await asyncio.wait_for(results.get(), timeout=30)
                    assert not errors
                    assert result == direct
                    actual.append(
                        f"{row['id']}|{result.class_key}|{result.key_version}\n"
                    )
                    expected.append(
                        f"{row['id']}|{direct.class_key}|{direct.key_version}\n"
                    )
        finally:
            await stop_handler()
            await stop_observe()
    finally:
        await bus.close()
    assert len(actual) == manifest["row_count"]
    assert "".join(actual).encode() == "".join(expected).encode()
    target = os.environ.get("ONEX_ERROR_NORMALIZER_KEYS_FILE")
    if target:
        Path(target).write_text("".join(actual))
