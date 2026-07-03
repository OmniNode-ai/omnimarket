# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Full declared-state COMPUTE coverage for node_checkpoint_compute, driven over
the canonical in-memory bus.

OMN-13674 (cluster wave-sweep-audit-compute). The COMPUTE handler
``HandlerCheckpointCompute`` is dispatched through ``LocalRuntimeBusAdapter`` over
``EventBusInmemory`` (via the ``integration_event_bus`` fixture): a
``ModelCheckpointRequest`` lands on the contract-declared command topic
``onex.cmd.omnimarket.checkpoint-start.v1`` and the runtime auto-emits the
``ModelCheckpointResult`` onto the contract-declared terminal topic
``onex.evt.omnimarket.checkpoint-completed.v1``.

The node is an impure COMPUTE (declares ``runtime_profiles: [effects]``): it
reads/writes checkpoint projections under the Onex state directory. The
filesystem boundary is bound by CONSTRUCTOR INJECTION -- the handler is built
with a ``state_dir`` pointing at a pytest ``tmp_path`` -- so no monkeypatching of
``open``/IO primitives, and nothing touches a real state directory.

COMPUTE DoD:
  * every declared action verdict reached -- save / load / list -- asserted on
    the terminal ``action`` / ``data`` / ``checkpoint_list`` fields;
  * every branch exercised: save-then-load round trip, load of a missing id
    (``data is None``), list ordering, case-insensitive action, and
    save-overwrite idempotency (duplicate + out-of-order writes);
  * negative controls: a ``save`` with no payload and an unknown action both
    raise ``ValueError`` in the pure handler and emit NO terminal event over the
    bus (the runtime adapter records the failure and publishes nothing).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_checkpoint_compute.handlers.handler_checkpoint_compute import (
    HandlerCheckpointCompute,
)
from omnimarket.nodes.node_checkpoint_compute.models.model_checkpoint_request import (
    ModelCheckpointRequest,
)
from omnimarket.nodes.node_checkpoint_compute.models.model_checkpoint_result import (
    ModelCheckpointResult,
)
from tests.runtime_local_compat import LocalRuntimeBusAdapter

# Contract-declared topics (node_checkpoint_compute/contract.yaml).
_START_TOPIC = "onex.cmd.omnimarket.checkpoint-start.v1"
_COMPLETED_TOPIC = "onex.evt.omnimarket.checkpoint-completed.v1"


async def _subscribe(
    bus: Any, handler: HandlerCheckpointCompute
) -> LocalRuntimeBusAdapter:
    """Wire the handler onto the declared command topic via the runtime adapter."""
    adapter = LocalRuntimeBusAdapter(
        handler=handler,
        handler_name="checkpoint-compute",
        input_model_cls=ModelCheckpointRequest,
        output_topic=_COMPLETED_TOPIC,
        bus=bus,
    )
    await bus.subscribe(
        _START_TOPIC,
        on_message=adapter.on_message,
        group_id="omnimarket-checkpoint-test",
    )
    return adapter


async def _publish(bus: Any, request: ModelCheckpointRequest) -> None:
    await bus.publish(
        _START_TOPIC,
        key=None,
        value=request.model_dump_json().encode("utf-8"),
    )


async def _terminal_events(bus: Any) -> list[ModelCheckpointResult]:
    history = await bus.get_event_history(topic=_COMPLETED_TOPIC)
    return [ModelCheckpointResult.model_validate(json.loads(e.value)) for e in history]


@pytest.mark.integration
async def test_checkpoint_save_load_list_over_bus(
    integration_event_bus: Any, tmp_path: Path
) -> None:
    """Save then load round-trips the payload; list returns saved ids in order.

    All three declared actions transit the bus against one injected
    ``state_dir``, so the terminal payloads reflect real persisted projections.
    """
    bus = integration_event_bus
    await bus.start()
    try:
        handler = HandlerCheckpointCompute(state_dir=tmp_path)
        await _subscribe(bus, handler)

        await _publish(
            bus,
            ModelCheckpointRequest(
                checkpoint_id="cp-alpha",
                action="save",
                payload={"phase": "one", "count": 3},
            ),
        )
        await _publish(
            bus,
            ModelCheckpointRequest(
                checkpoint_id="cp-beta",
                action="save",
                payload={"phase": "two"},
            ),
        )
        await _publish(
            bus,
            ModelCheckpointRequest(checkpoint_id="cp-alpha", action="load"),
        )
        await _publish(
            bus,
            ModelCheckpointRequest(checkpoint_id="cp-alpha", action="list"),
        )

        events = await _terminal_events(bus)
        assert [e.action for e in events] == ["save", "save", "load", "list"]

        # save verdicts: data is None, no list.
        assert events[0].action == "save"
        assert events[0].data is None
        assert events[0].checkpoint_id == "cp-alpha"

        # load verdict: round-tripped payload.
        loaded = events[2]
        assert loaded.action == "load"
        assert loaded.data == {"phase": "one", "count": 3}

        # list verdict: both ids in stable sorted order.
        listed = events[3]
        assert listed.action == "list"
        assert listed.checkpoint_list == ["cp-alpha", "cp-beta"]
        assert listed.data is None
    finally:
        await bus.close()


@pytest.mark.integration
async def test_checkpoint_load_missing_returns_null_data_over_bus(
    integration_event_bus: Any, tmp_path: Path
) -> None:
    """Loading an id that was never saved yields ``data is None`` (not an error)."""
    bus = integration_event_bus
    await bus.start()
    try:
        handler = HandlerCheckpointCompute(state_dir=tmp_path)
        await _subscribe(bus, handler)
        await _publish(
            bus,
            ModelCheckpointRequest(checkpoint_id="cp-missing", action="load"),
        )
        events = await _terminal_events(bus)
        assert len(events) == 1
        assert events[0].action == "load"
        assert events[0].data is None
    finally:
        await bus.close()


@pytest.mark.integration
async def test_checkpoint_case_insensitive_action_over_bus(
    integration_event_bus: Any, tmp_path: Path
) -> None:
    """Actions are normalized (lower/stripped): ``SAVE`` persists like ``save``."""
    bus = integration_event_bus
    await bus.start()
    try:
        handler = HandlerCheckpointCompute(state_dir=tmp_path)
        await _subscribe(bus, handler)
        await _publish(
            bus,
            ModelCheckpointRequest(
                checkpoint_id="cp-mixed",
                action="  SAVE  ",
                payload={"ok": True},
            ),
        )
        await _publish(
            bus,
            ModelCheckpointRequest(checkpoint_id="cp-mixed", action="Load"),
        )
        events = await _terminal_events(bus)
        assert events[0].action == "save"
        assert events[1].action == "load"
        assert events[1].data == {"ok": True}
    finally:
        await bus.close()


@pytest.mark.integration
async def test_checkpoint_overwrite_idempotency_over_bus(
    integration_event_bus: Any, tmp_path: Path
) -> None:
    """Duplicate + out-of-order saves of the same id overwrite deterministically;
    list stays deduplicated and load returns the last-written payload."""
    bus = integration_event_bus
    await bus.start()
    try:
        handler = HandlerCheckpointCompute(state_dir=tmp_path)
        await _subscribe(bus, handler)

        # Out-of-order / duplicate writes to the same key.
        await _publish(
            bus,
            ModelCheckpointRequest(
                checkpoint_id="cp-dup", action="save", payload={"v": 1}
            ),
        )
        await _publish(
            bus,
            ModelCheckpointRequest(
                checkpoint_id="cp-dup", action="save", payload={"v": 2}
            ),
        )
        await _publish(
            bus,
            ModelCheckpointRequest(checkpoint_id="cp-dup", action="list"),
        )
        await _publish(
            bus,
            ModelCheckpointRequest(checkpoint_id="cp-dup", action="load"),
        )

        events = await _terminal_events(bus)
        listed = next(e for e in events if e.action == "list")
        loaded = next(e for e in events if e.action == "load")
        # A single id despite two writes -> idempotent projection key.
        assert listed.checkpoint_list == ["cp-dup"]
        # Last write wins.
        assert loaded.data == {"v": 2}
    finally:
        await bus.close()


@pytest.mark.integration
async def test_checkpoint_save_without_payload_raises_and_emits_no_terminal(
    integration_event_bus: Any, tmp_path: Path
) -> None:
    """Negative control: a ``save`` with no payload raises ValueError in the pure
    handler and produces NO terminal event over the bus."""
    handler = HandlerCheckpointCompute(state_dir=tmp_path)

    # Pure handler raises (typed failure, not a silent return).
    with pytest.raises(ValueError, match="payload is required"):
        handler.handle(
            ModelCheckpointRequest(checkpoint_id="cp-bad", action="save", payload=None)
        )

    bus = integration_event_bus
    await bus.start()
    try:
        await _subscribe(bus, handler)
        await _publish(
            bus,
            ModelCheckpointRequest(checkpoint_id="cp-bad", action="save", payload=None),
        )
        # The adapter swallows the handler failure: no terminal event is emitted.
        assert await _terminal_events(bus) == []
    finally:
        await bus.close()


@pytest.mark.integration
async def test_checkpoint_unknown_action_raises_and_emits_no_terminal(
    integration_event_bus: Any, tmp_path: Path
) -> None:
    """Negative control: an unknown action raises ValueError and emits no terminal
    event over the bus."""
    handler = HandlerCheckpointCompute(state_dir=tmp_path)

    with pytest.raises(ValueError, match="save, load, list"):
        handler.handle(
            ModelCheckpointRequest(checkpoint_id="cp-x", action="delete", payload=None)
        )

    bus = integration_event_bus
    await bus.start()
    try:
        await _subscribe(bus, handler)
        await _publish(
            bus,
            ModelCheckpointRequest(checkpoint_id="cp-x", action="delete"),
        )
        assert await _terminal_events(bus) == []
    finally:
        await bus.close()
