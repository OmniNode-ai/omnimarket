# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain event tests of the host storage effect (OMN-22906), on the in-memory bus."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_host_storage_effect.handlers.handler_host_storage import (
    HandlerHostStorage,
    select_backend,
)
from omnimarket.nodes.node_host_storage_effect.models.model_host_storage import (
    EnumHostStorageOutcome,
    EnumHostStorageRefusal,
    ModelHostStorageRequest,
    ModelHostStorageResult,
)
from omnimarket.nodes.node_host_storage_effect.models.model_host_storage_overlay import (
    ModelHostStorageOverlay,
)

pytestmark = pytest.mark.unit

NODE_DIR = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_host_storage_effect"
)
CONTRACT = yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))
CMD = CONTRACT["event_bus"]["subscribe_topics"][0]
EVT = CONTRACT["event_bus"]["publish_topics"][0]


async def chain(
    request: ModelHostStorageRequest, *, drop_result: bool = False
) -> list[ModelHostStorageResult]:
    overlay = ModelHostStorageOverlay.model_validate(
        yaml.safe_load(
            (NODE_DIR / "overlays/in_memory.yaml").read_text(encoding="utf-8")
        )
    )
    handler = HandlerHostStorage(overlay, select_backend(overlay))
    bus = EventBusInmemory()
    await bus.start()
    seen: list[ModelHostStorageResult] = []

    async def node(msg: Any) -> None:
        result = handler.handle(ModelHostStorageRequest.model_validate_json(msg.value))
        if not drop_result:
            await bus.publish(EVT, key=None, value=result.model_dump_json().encode())

    async def floor(msg: Any) -> None:
        seen.append(ModelHostStorageResult.model_validate_json(msg.value))

    await bus.subscribe(CMD, on_message=node, group_id="host-storage-node")
    await bus.subscribe(EVT, on_message=floor, group_id="host-storage-reader")
    await bus.publish(CMD, key=None, value=request.model_dump_json().encode())
    await bus.close()
    return seen


def test_host_storage_topics_follow_the_naming_rule_and_the_terminal_event() -> None:
    assert CMD.startswith("onex.cmd.")
    assert CMD.endswith(".v1")
    assert EVT.startswith("onex.evt.")
    assert EVT.endswith(".v1")
    assert CONTRACT["terminal_event"] == EVT


async def test_host_storage_golden_chain_a_command_yields_one_typed_result_event() -> (
    None
):
    seen = await chain(
        ModelHostStorageRequest(
            host="sim-host-a", command="prune_temp", requested_by="lane-a"
        )
    )
    assert len(seen) == 1
    assert seen[0].outcome is EnumHostStorageOutcome.OK
    assert (seen[0].free_bytes_before, seen[0].free_bytes_after) == (20000, 25000)


async def test_host_storage_error_chain_an_undeclared_command_yields_a_typed_refusal_event() -> (
    None
):
    seen = await chain(
        ModelHostStorageRequest(
            host="sim-host-b", command="prune_temp", requested_by="lane-a"
        )
    )
    assert len(seen) == 1
    assert seen[0].outcome is EnumHostStorageOutcome.REFUSED
    assert seen[0].refusal is EnumHostStorageRefusal.COMMAND_NOT_DECLARED_FOR_HOST


async def test_host_storage_error_chain_reading_has_teeth_with_the_result_event_dropped() -> (
    None
):
    """Injected break: the node consumes the command and publishes no result. The reading must see it."""
    seen = await chain(
        ModelHostStorageRequest(
            host="sim-host-b", command="prune_temp", requested_by="lane-a"
        ),
        drop_result=True,
    )
    with pytest.raises(AssertionError):
        assert len(seen) == 1
