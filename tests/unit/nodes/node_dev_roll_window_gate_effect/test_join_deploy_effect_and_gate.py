# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Join test: node_redeploy_deploy_effect and node_dev_roll_window_gate_effect, the seam between them.

The real deploy effect (a topic-recording bus) hands each dev command to the hold topic while the gate
overlay is live; the real gate handlers hold them and, after the window, release them back; the effect
publishes the released commands to the deploy agent. Three merges inside one window are three held
commands and no rebuild; one tick after the window is three rebuild commands in merge order, each marked
released; a second burst while the first roll has no completion publishes nothing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick
from pydantic import BaseModel

from omnimarket.events.runtime_deployment import ModelDeployPublishCommand
from omnimarket.nodes.node_redeploy_deploy_effect.handlers.handler_deploy_publish_monitor import (
    TOPIC_DEV_ROLL_HOLD,
    TOPIC_REBUILD_REQUESTED,
    HandlerDeployPublishMonitor,
)
from tests.unit.nodes.node_dev_roll_window_gate_effect.chain_support import (
    DEPLOY_PUBLISH_TOPIC,
    HOLD_TOPIC,
    tick_payload,
)
from tests.unit.nodes.node_dev_roll_window_gate_effect.conftest import (
    OVERLAY_DOC,
    WINDOW,
    Gate,
    command,
    completed,
)

pytestmark = pytest.mark.unit


class TopicBus:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []

    async def publish(self, topic: str, **kwargs: Any) -> None:
        self.sent.append((topic, json.loads(kwargs["value"])))

    def on(self, topic: str) -> list[dict[str, Any]]:
        return [p for t, p in self.sent if t == topic]


class Pair:
    """The effect and the gate wired the way the runtime wires them: by topic."""

    def __init__(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gate: Gate
    ) -> None:
        monkeypatch.setenv("ONEX_STATE_DIR", str(tmp_path / "state"))
        monkeypatch.setenv("DEPLOY_AGENT_HMAC_SECRET", "unit-test-signing-secret")
        root = tmp_path / "overlays"
        (root / "node_dev_roll_window_gate_effect").mkdir(parents=True)
        (root / "node_dev_roll_window_gate_effect" / "overlay.yaml").write_text(
            yaml.safe_dump({**OVERLAY_DOC, "store": {"backend": "in_memory"}})
        )
        monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(root))
        self.bus = TopicBus()
        self.effect = HandlerDeployPublishMonitor(self.bus)
        self.gate = gate
        self._forwarded = 0
        self.rebuilds_seen = 0

    async def deploy(self, item: ModelDeployPublishCommand) -> None:
        await self.effect.handle(
            ModelEventEnvelope(
                payload=item,
                correlation_id=item.correlation_id,
                event_type=DEPLOY_PUBLISH_TOPIC,
            )
        )
        await self._pump()

    async def tick(self) -> list[BaseModel]:
        out = self.gate.tick.handle(
            ModelRuntimeTick.model_validate(tick_payload(self.gate.clock.now))
        )
        for model in out or []:
            if isinstance(model, ModelDeployPublishCommand):
                await self.effect.handle(
                    ModelEventEnvelope(
                        payload=model,
                        correlation_id=model.correlation_id,
                        event_type=DEPLOY_PUBLISH_TOPIC,
                    )
                )
        return list(out or [])

    async def _pump(self) -> None:
        held = self.bus.on(TOPIC_DEV_ROLL_HOLD)
        for payload in held[self._forwarded :]:
            self.gate.hold.handle(ModelDeployPublishCommand.model_validate(payload))
        self._forwarded = len(held)

    def rebuilds(self) -> list[dict[str, Any]]:
        return self.bus.on(TOPIC_REBUILD_REQUESTED)


def test_the_two_nodes_agree_on_the_hold_topic_by_their_contracts() -> None:
    """The effect publishes where the gate subscribes: the seam is one topic declared in both contracts."""
    assert TOPIC_DEV_ROLL_HOLD == HOLD_TOPIC


async def test_three_merges_in_one_window_are_one_batch_of_three_rebuilds_in_merge_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pair = Pair(tmp_path, monkeypatch, Gate())
    sent = [command(number=n, age_seconds=WINDOW - 60 * n) for n in (1, 2, 3)]
    for item in sent:
        await pair.deploy(item)
    assert pair.rebuilds() == []
    assert len(pair.bus.on(TOPIC_DEV_ROLL_HOLD)) == 3

    pair.gate.clock.advance(WINDOW)
    await pair.tick()
    rebuilds = pair.rebuilds()
    assert [r["correlation_id"] for r in rebuilds] == [
        str(c.correlation_id) for c in sent
    ]
    assert (
        len(pair.bus.on(TOPIC_DEV_ROLL_HOLD)) == 3
    )  # a released command is not held again


async def test_a_second_burst_while_the_first_roll_has_no_completion_publishes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pair = Pair(tmp_path, monkeypatch, Gate())
    for n in (1, 2):
        await pair.deploy(command(number=n, age_seconds=WINDOW))
    await pair.tick()
    assert len(pair.rebuilds()) == 2
    for n in (3, 4):
        await pair.deploy(command(number=n, age_seconds=WINDOW))
    pair.gate.clock.advance(WINDOW * 4)
    out = await pair.tick()
    assert len(pair.rebuilds()) == 2
    assert [m.hold_reason for m in out if hasattr(m, "hold_reason")] == [
        "roll_in_progress"
    ]


async def test_after_the_completion_and_the_interval_the_second_burst_is_released_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pair = Pair(tmp_path, monkeypatch, Gate())
    first = command(number=1, age_seconds=WINDOW)
    await pair.deploy(first)
    await pair.tick()
    for n in (3, 4):
        await pair.deploy(command(number=n, age_seconds=WINDOW))
    pair.gate.clock.advance(120)
    pair.gate.completed.handle(completed(first.correlation_id, at=pair.gate.clock.now))
    pair.gate.clock.advance(WINDOW)
    await pair.tick()
    assert len(pair.rebuilds()) == 3


async def test_a_command_the_gate_cannot_hold_still_reaches_the_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail open: a dev command with a branch alias for its ref is refused by the gate and passed on, never dropped."""
    pair = Pair(tmp_path, monkeypatch, Gate())
    odd = command(git_ref="origin/dev")
    await pair.deploy(odd)
    out = pair.gate.hold.handle(
        ModelDeployPublishCommand.model_validate(pair.bus.on(TOPIC_DEV_ROLL_HOLD)[0])
    )
    assert isinstance(out, list)
    passed = out[1]
    assert isinstance(passed, ModelDeployPublishCommand)
    await pair.effect.handle(
        ModelEventEnvelope(
            payload=passed,
            correlation_id=passed.correlation_id,
            event_type=DEPLOY_PUBLISH_TOPIC,
        )
    )
    assert [r["correlation_id"] for r in pair.rebuilds()] == [str(odd.correlation_id)]


async def test_failing_control_with_no_gate_overlay_the_first_command_is_published_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The coalescing is real: with the gate overlay absent the same first command goes straight to the agent."""
    pair = Pair(tmp_path, monkeypatch, Gate())
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path / "none"))
    await pair.deploy(command(number=1, age_seconds=WINDOW))
    assert len(pair.rebuilds()) == 1
    with pytest.raises(AssertionError):
        assert pair.rebuilds() == []
