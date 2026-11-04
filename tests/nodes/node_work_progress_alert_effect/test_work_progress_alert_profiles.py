# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC6: overlay values change evaluations, subjects and findings under each profile."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from uuid import uuid4

import pytest
import yaml
from omnibase_infra.event_bus.models.model_event_headers import ModelEventHeaders
from omnibase_infra.event_bus.models.model_event_message import ModelEventMessage

from omnimarket.nodes.node_work_progress_alert_effect import NodeWorkProgressAlertEffect
from omnimarket.nodes.node_work_progress_alert_effect.models import (
    EnumWorkProgressOutcome,
    ModelLedgerRow,
    ModelWorkProgressAlertTrigger,
    ModelWorkProgressEvaluated,
)
from tests.nodes.node_work_progress_alert_effect.support import (
    BUDGETS,
    SETTINGS,
    Clock,
    Reader,
    install_overlays,
    resolve_channel,
    row,
)

HEARTBEAT = "onex.evt.platform.node-heartbeat.v1"


class ProtocolProfileBus(Protocol):
    async def publish(self, topic: str, key: bytes | None, value: bytes) -> object: ...

    async def subscribe_ungrouped(
        self,
        topic: str,
        on_message: Callable[[ModelEventMessage], Awaitable[None]],
    ) -> Callable[[], Awaitable[None]]: ...


class InMemoryBus:
    """Publishing calls the topic's subscribers in registration order."""

    def __init__(self) -> None:
        self.subscribers: dict[
            str, list[Callable[[ModelEventMessage], Awaitable[None]]]
        ] = {}

    async def publish(self, topic: str, key: bytes | None, value: bytes) -> object:
        message = ModelEventMessage(
            topic=topic,
            key=key,
            value=value,
            headers=ModelEventHeaders(
                source="work-progress-alert-test",
                event_type=topic,
                timestamp=datetime.now(UTC),
            ),
        )
        for callback in list(self.subscribers.get(topic, [])):
            await callback(message)
        return None

    async def subscribe_ungrouped(
        self,
        topic: str,
        on_message: Callable[[ModelEventMessage], Awaitable[None]],
    ) -> Callable[[], Awaitable[None]]:
        self.subscribers.setdefault(topic, []).append(on_message)

        async def unsubscribe() -> None:
            self.subscribers[topic].remove(on_message)

        return unsubscribe


def _topics() -> tuple[str, str, str]:
    contract = yaml.safe_load(
        (
            Path(__file__).resolve().parents[3]
            / "src/omnimarket/nodes/node_work_progress_alert_effect/contract.yaml"
        ).read_text()
    )
    bus = contract["event_bus"]
    assert bus["subscribe_topics"] == [HEARTBEAT]
    suffix = uuid4().hex[:12]

    def unique(topic: str) -> str:
        return f"{topic.removesuffix('.v1')}.{suffix}.v1"

    slack = next(t for t in bus["publish_topics"] if "slack-publish" in t)
    return unique(HEARTBEAT), unique(slack), unique(contract["terminal_event"])


async def _heartbeat_over(
    bus: ProtocolProfileBus,
    node: NodeWorkProgressAlertEffect,
    sent: list[tuple[str, bytes]],
) -> tuple[ModelWorkProgressEvaluated, list[dict[str, object]]]:
    heartbeat, slack, terminal = _topics()
    terminals: asyncio.Queue[ModelWorkProgressEvaluated] = asyncio.Queue()
    commands: list[dict[str, object]] = []

    async def on_heartbeat(message: ModelEventMessage) -> None:
        sent.clear()
        evaluated = node.handle(
            ModelWorkProgressAlertTrigger.model_validate_json(message.value)
        )
        for topic, payload in sent:
            if topic.endswith("slack-publish.v1"):
                await bus.publish(slack, None, payload)
        await bus.publish(terminal, None, evaluated.model_dump_json().encode())

    async def on_slack(message: ModelEventMessage) -> None:
        import json

        commands.append(json.loads(message.value))

    async def on_terminal(message: ModelEventMessage) -> None:
        await terminals.put(
            ModelWorkProgressEvaluated.model_validate_json(message.value)
        )

    stops = [
        await bus.subscribe_ungrouped(terminal, on_terminal),
        await bus.subscribe_ungrouped(slack, on_slack),
        await bus.subscribe_ungrouped(heartbeat, on_heartbeat),
    ]
    try:
        await bus.publish(heartbeat, None, b"{}")
        result = await asyncio.wait_for(terminals.get(), timeout=30)
    finally:
        for stop in reversed(stops):
            await stop()
    return result, commands


def _rows() -> list[ModelLedgerRow]:
    # lane-a: placed 6000 s ago (over 900). lane-b: claimed 3000 s ago (under 3600).
    # ctl-1: placed 6000 s ago, ignorable by lane glob.
    return [
        row(6000, "CLAIM", "lane-a", host="h051", parent="orch-1"),
        row(7000, "CLAIM", "lane-b", host="h051"),
        row(3000, "STATUS", "lane-b", note="x"),
        row(6000, "CLAIM", "ctl-1", host="h051"),
        row(100000, "CLAIM", "old-lane", host="h051"),
    ]


async def _assert_profile(
    bus: ProtocolProfileBus, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    resolve_channel(monkeypatch)
    sent: list[tuple[str, bytes]] = []

    def publisher(topic: str, payload: bytes) -> None:
        sent.append((topic, payload))

    async def run(
        settings: dict[str, object], budgets: dict[str, object] = BUDGETS
    ) -> tuple[ModelWorkProgressEvaluated, list[dict[str, object]]]:
        install_overlays(monkeypatch, tmp_path, settings=settings, budgets=budgets)
        node = NodeWorkProgressAlertEffect(
            reader=Reader(_rows()), event_publisher=publisher, clock=Clock()
        )
        return await _heartbeat_over(bus, node, sent)

    base, base_commands = await run(SETTINGS)
    assert (base.subjects, base.findings, base.commands) == (2, 1, 1)
    assert base.check_interval_seconds == 300
    assert str(base_commands[0]["idempotency_key"]).startswith(
        "work-progress|lane:lane-a|placed|"
    )

    # check_interval_seconds changes the evaluations: a second heartbeat 400 s
    # later opens a new 300 s bucket but falls inside the same 900 s bucket.
    for interval, expected in (
        (300, EnumWorkProgressOutcome.EVALUATED),
        (900, EnumWorkProgressOutcome.THROTTLED),
    ):
        install_overlays(
            monkeypatch,
            tmp_path,
            settings={**SETTINGS, "check_interval_seconds": interval},
        )
        clock = Clock()
        node = NodeWorkProgressAlertEffect(
            reader=Reader(_rows()), event_publisher=publisher, clock=clock
        )
        first, _ = await _heartbeat_over(bus, node, sent)
        assert first.outcome is EnumWorkProgressOutcome.EVALUATED
        clock.advance(400)
        second, _ = await _heartbeat_over(bus, node, sent)
        assert second.outcome is expected, interval

    # lookback_seconds changes the subjects.
    wide, _ = await run({**SETTINGS, "lookback_seconds": 200000})
    narrow, _ = await run({**SETTINGS, "lookback_seconds": 6500})
    assert (wide.subjects, narrow.subjects) == (3, 1)

    # ignore changes the subjects (and so the findings).
    ignoring_lane, commands = await run(
        {**SETTINGS, "ignore": {"lanes": ["lane-*", "ctl-*"], "actors": []}}
    )
    assert (ignoring_lane.subjects, ignoring_lane.findings) == (0, 0)
    assert commands == []

    # one compute budget changes the findings.
    generous, _ = await run(
        SETTINGS, {"budget_seconds": {"placed": 7000, "claimed": 3600, "pr_open": 5400}}
    )
    assert (generous.subjects, generous.findings) == (2, 0)


async def test_work_progress_alert_in_memory_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    await _assert_profile(InMemoryBus(), monkeypatch, tmp_path)


@pytest.mark.kafka
@pytest.mark.integration
async def test_work_progress_alert_redpanda_overlay_profile(
    kafka_integration_bus: ProtocolProfileBus,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    await _assert_profile(kafka_integration_bus, monkeypatch, tmp_path)
