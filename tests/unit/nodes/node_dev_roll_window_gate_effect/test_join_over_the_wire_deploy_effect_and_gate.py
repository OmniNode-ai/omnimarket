# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Join over the wire: the deploy effect's own published bytes through the gate's runtime dispatch and back.

The in-process join test hands models between the handlers. This one keeps the bytes: the deploy effect
publishes the hold message with its own bus call, those exact bytes are sent to the gate's real contract on
the in-memory bus (RuntimeDispatch refuses a bare payload, so a hold message that is not an envelope fails
here), and the commands the gate releases are read back as bytes and handed to the effect, which publishes
them to the deploy agent.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

from omnimarket.nodes.node_redeploy_deploy_effect.handlers.handler_deploy_publish_monitor import (
    TOPIC_DEV_ROLL_HOLD,
    TOPIC_REBUILD_REQUESTED,
    HandlerDeployPublishMonitor,
)
from tests.unit.nodes.node_dev_roll_window_gate_effect.chain_support import (
    DEPLOY_PUBLISH_TOPIC,
    HELD_TOPIC,
    HOLD_TOPIC,
    KEY,
    REFUSED_TOPIC,
    TICK_TOPIC,
    chain,
    envelope,
    on,
    tick_payload,
)
from tests.unit.nodes.node_dev_roll_window_gate_effect.conftest import (
    OVERLAY_DOC,
    WINDOW,
    Gate,
    command,
)

pytestmark = pytest.mark.unit


class WireBus:
    """Keeps the exact bytes of every publish."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, bytes]] = []

    async def publish(self, topic: str, **kwargs: Any) -> None:
        self.sent.append((topic, kwargs["value"]))

    def on(self, topic: str) -> list[bytes]:
        return [value for t, value in self.sent if t == topic]


def install_overlay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ONEX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("DEPLOY_AGENT_HMAC_SECRET", "unit-test-signing-secret")
    root = tmp_path / "overlays"
    (root / "node_dev_roll_window_gate_effect").mkdir(parents=True)
    (root / "node_dev_roll_window_gate_effect" / "overlay.yaml").write_text(
        yaml.safe_dump({**OVERLAY_DOC, "store": {"backend": "in_memory"}})
    )
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(root))


async def test_the_effects_hold_bytes_are_accepted_by_the_gate_and_the_release_reaches_the_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_overlay(tmp_path, monkeypatch)
    wire = WireBus()
    effect = HandlerDeployPublishMonitor(wire)
    gate = Gate()
    dispatch, transport, broker, _ = chain(gate)
    sent = [command(number=n, age_seconds=WINDOW - 60 * n) for n in (1, 2)]
    for item in sent:
        await effect.handle(
            ModelEventEnvelope(
                payload=item,
                correlation_id=item.correlation_id,
                event_type=DEPLOY_PUBLISH_TOPIC,
            )
        )
    held_bytes = wire.on(TOPIC_DEV_ROLL_HOLD)
    assert len(held_bytes) == 2
    assert wire.on(TOPIC_REBUILD_REQUESTED) == []
    for value in held_bytes:
        await transport.send(HOLD_TOPIC, key=KEY, value=value, headers={})
    assert await dispatch.drain() == 2
    assert len(on(broker, HELD_TOPIC)) == 2
    assert on(broker, REFUSED_TOPIC) == []

    gate.clock.advance(WINDOW)
    await transport.send(
        TICK_TOPIC,
        key=KEY,
        value=envelope(tick_payload(gate.clock.now), "ModelRuntimeTick"),
        headers={},
    )
    assert await dispatch.drain() == 1
    released = [
        ModelEventEnvelope[object].model_validate_json(record.value)
        for record in broker.records(DEPLOY_PUBLISH_TOPIC, 0)
    ]
    assert len(released) == 2
    for message in released:
        await effect.handle(message)
    rebuilds = [json.loads(value) for value in wire.on(TOPIC_REBUILD_REQUESTED)]
    assert [r["correlation_id"] for r in rebuilds] == [
        str(c.correlation_id) for c in sent
    ]
    assert (
        len(wire.on(TOPIC_DEV_ROLL_HOLD)) == 2
    )  # the released commands were not held again


async def test_failing_control_a_bare_payload_on_the_hold_topic_is_refused_by_the_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The envelope matters: the same command as bare JSON is not held (the runtime refuses it)."""
    install_overlay(tmp_path, monkeypatch)
    gate = Gate()
    dispatch, transport, broker, _ = chain(gate)
    item = command(number=1)
    await transport.send(
        HOLD_TOPIC,
        key=KEY,
        value=item.model_dump_json().encode(),
        headers={},
    )
    await dispatch.drain()
    assert on(broker, HELD_TOPIC) == []
    assert gate.store.load().held == ()
