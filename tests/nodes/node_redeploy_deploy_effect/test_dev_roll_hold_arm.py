# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The deploy effect's hold arm (OMN-19998): a dev command goes to the roll window gate only while its overlay is live."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

from omnimarket.events.runtime_deployment import (
    EnumRuntimeLane,
    ModelDeployPublishCommand,
)
from omnimarket.nodes.node_redeploy_deploy_effect.handlers.handler_deploy_publish_monitor import (
    TOPIC_DEV_ROLL_HOLD,
    TOPIC_REBUILD_REQUESTED,
    HandlerDeployPublishMonitor,
)

GATE_NODE = "node_dev_roll_window_gate_effect"
DEPLOY_PUBLISH_TOPIC = "onex.cmd.omnimarket.redeploy-deploy-publish.v1"
GATE_OVERLAY: dict[str, Any] = {
    "mode": "live",
    "issuer_repository_pattern": r"^gha/(?P<repository>[a-z0-9_]+)/pr-[0-9]+$",
    "batch_window_seconds": 900,
    "min_start_interval_seconds": 900,
    "healthy_for_seconds": 600,
    "failed_roll_release_after_seconds": 3600,
    "known_repositories": ["omnimarket", "omnibase_infra"],
    "build_context_repository": "omnibase_infra",
    "fallback_git_ref": "origin/dev",
    "requested_by": "dev-roll-window-gate",
    "held_kept": 10,
    "store": {"backend": "in_memory"},
}


class TopicBus:
    """Records every publish with its topic."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []

    async def publish(self, topic: str, **kwargs: Any) -> None:
        self.sent.append((topic, json.loads(kwargs["value"])))

    def on(self, topic: str) -> list[dict[str, Any]]:
        return [payload for t, payload in self.sent if t == topic]


def command(
    *,
    lane: EnumRuntimeLane = EnumRuntimeLane.DEV,
    released: bool = False,
) -> ModelDeployPublishCommand:
    return ModelDeployPublishCommand(
        correlation_id=uuid4(),
        runtime_lane=lane,
        requested_by="gha/omnimarket/pr-12",
        git_ref="a" * 40,
        requested_at=datetime(2026, 11, 14, 12, 0, tzinfo=UTC),
        roll_window_released=released,
    )


def envelope(item: ModelDeployPublishCommand) -> ModelEventEnvelope[Any]:
    return ModelEventEnvelope(
        payload=item,
        correlation_id=item.correlation_id,
        event_type=DEPLOY_PUBLISH_TOPIC,
    )


def overlay_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, doc: dict[str, Any] | None
) -> None:
    """Install (or leave absent) the gate overlay and point the effect's state at tmp."""
    monkeypatch.setenv("ONEX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("DEPLOY_AGENT_HMAC_SECRET", "unit-test-signing-secret")
    root = tmp_path / "overlays"
    root.mkdir()
    if doc is not None:
        (root / GATE_NODE).mkdir()
        (root / GATE_NODE / "overlay.yaml").write_text(yaml.safe_dump(doc))
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(root))


pytestmark = pytest.mark.unit


async def test_live_gate_sends_a_dev_command_to_the_hold_topic_and_never_to_the_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    overlay_env(tmp_path, monkeypatch, GATE_OVERLAY)
    bus = TopicBus()
    item = command()
    await HandlerDeployPublishMonitor(bus).handle(envelope(item))
    assert [t for t, _ in bus.sent] == [TOPIC_DEV_ROLL_HOLD]
    assert bus.on(TOPIC_REBUILD_REQUESTED) == []
    assert bus.on(TOPIC_DEV_ROLL_HOLD)[0]["correlation_id"] == str(item.correlation_id)


async def test_a_released_command_is_published_to_the_agent_and_not_held_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    overlay_env(tmp_path, monkeypatch, GATE_OVERLAY)
    bus = TopicBus()
    item = command(released=True)
    await HandlerDeployPublishMonitor(bus).handle(envelope(item))
    assert bus.on(TOPIC_DEV_ROLL_HOLD) == []
    (sent,) = bus.on(TOPIC_REBUILD_REQUESTED)
    assert sent["correlation_id"] == str(item.correlation_id)


@pytest.mark.parametrize("doc", [{**GATE_OVERLAY, "mode": "off"}, None])
async def test_gate_off_or_absent_publishes_exactly_as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, doc: dict[str, Any] | None
) -> None:
    overlay_env(tmp_path, monkeypatch, doc)
    bus = TopicBus()
    await HandlerDeployPublishMonitor(bus).handle(envelope(command()))
    assert bus.on(TOPIC_DEV_ROLL_HOLD) == []
    assert len(bus.on(TOPIC_REBUILD_REQUESTED)) == 1


async def test_a_non_dev_command_is_never_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    overlay_env(tmp_path, monkeypatch, GATE_OVERLAY)
    bus = TopicBus()
    await HandlerDeployPublishMonitor(bus).handle(
        envelope(command(lane=EnumRuntimeLane.STABILITY_TEST))
    )
    assert bus.on(TOPIC_DEV_ROLL_HOLD) == []
    assert len(bus.on(TOPIC_REBUILD_REQUESTED)) == 1


async def test_an_installed_but_invalid_gate_overlay_raises_instead_of_turning_the_gate_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnimarket.handlers.node_overlay_reader import NodeOverlayError

    overlay_env(tmp_path, monkeypatch, {**GATE_OVERLAY, "batch_window_seconds": 0})
    with pytest.raises(NodeOverlayError):
        await HandlerDeployPublishMonitor(TopicBus()).handle(envelope(command()))


async def test_failing_control_a_live_gate_does_not_publish_to_the_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first test's claim is real: asserting that a live gate still publishes to the agent fails."""
    overlay_env(tmp_path, monkeypatch, GATE_OVERLAY)
    bus = TopicBus()
    await HandlerDeployPublishMonitor(bus).handle(envelope(command()))
    with pytest.raises(AssertionError):
        assert len(bus.on(TOPIC_REBUILD_REQUESTED)) == 1
