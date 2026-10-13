# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20590: a tick from the lane's own producer reaches a tick-driven handler.

The chain: ``start_lane_runtime_scheduler`` (main profile, lane switch on) ->
``RuntimeScheduler.emit_tick`` -> event bus -> auto-wired runtime-tick
subscription -> MessageDispatchEngine -> HandlerGitHubApiPoll -> result topic.

RED on the parent: no lane producer could be started at all (no
``start_lane_runtime_scheduler``, no lane switch), so no tick ever reached a
handler. The hand-built envelope in ``test_github_api_poll_golden_chain`` proves
the consumer half; this proves the producer the lanes actually run.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.errors import ProtocolConfigurationError
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.event_bus.models.model_event_message import ModelEventMessage
from omnibase_infra.runtime.auto_wiring import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import wire_from_manifest
from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine
from omnibase_infra.runtime.runtime_profile import resolve_runtime_profile_name
from omnibase_infra.runtime.runtime_scheduler import start_lane_runtime_scheduler
from omnibase_infra.topics import SUFFIX_RUNTIME_TICK

from omnimarket.nodes.node_github_pr_poller_effect.handlers import (
    handler_github_api_poll,
)
from omnimarket.nodes.node_github_pr_poller_effect.models.model_github_poller_config import (
    ModelGitHubPollerConfig,
)
from omnimarket.nodes.node_github_pr_poller_effect.models.model_github_poller_result import (
    ModelGitHubPollerResult,
)

_OUTPUT_TOPIC = "onex.evt.github.pr-status.v1"
_REPO = "OmniNode-ai/omnibase_infra"


def _contract_path() -> Path:
    return (
        Path(__file__).resolve().parents[3]
        / "src"
        / "omnimarket"
        / "nodes"
        / "node_github_pr_poller_effect"
        / "contract.yaml"
    )


class _FakeGitHubClient:
    def __init__(self, _token: str) -> None:
        pass

    def fetch_open_prs_for_triage(self, repo: str) -> list[dict[str, object]]:
        assert repo == _REPO
        return [
            {
                "number": 20590,
                "title": "Runtime tick producer",
                "draft": False,
                "labels": [],
                "updated_at": datetime.now(tz=UTC).isoformat().replace("+00:00", "Z"),
                "combined_status": "success",
                "review_states": ["APPROVED"],
            }
        ]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_lane_producer_tick_drives_a_tick_subscriber(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ONEX_RUNTIME_SCHEDULER_ENABLED", "true")
    monkeypatch.setenv("ONEX_RUNTIME_SCHEDULER_PERSIST_SEQUENCE", "false")
    # Long interval: the test drives one tick by hand, the loop adds none.
    monkeypatch.setenv("ONEX_RUNTIME_SCHEDULER_TICK_INTERVAL_MS", "60000")
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setattr(
        handler_github_api_poll,
        "_load_contract_config",
        lambda: ModelGitHubPollerConfig(
            repos=[_REPO],
            poll_interval_seconds=10,
            stale_threshold_hours=48,
        ),
    )
    monkeypatch.setattr(
        handler_github_api_poll, "GitHubHttpTransport", _FakeGitHubClient
    )

    manifest = discover_contracts_from_paths([_contract_path()])
    assert manifest.total_errors == 0

    bus = EventBusInmemory(environment="test", group="runtime-tick-producer-chain")
    await bus.start()
    try:
        observed: asyncio.Queue[ModelEventEnvelope[ModelGitHubPollerResult]] = (
            asyncio.Queue()
        )

        async def collect_result(message: ModelEventMessage) -> None:
            await observed.put(
                ModelEventEnvelope[ModelGitHubPollerResult].model_validate_json(
                    message.value
                )
            )

        await bus.subscribe(
            topic=_OUTPUT_TOPIC,
            group_id="runtime-tick-producer-result-collector",
            on_message=collect_result,
        )

        engine = MessageDispatchEngine()
        report = await wire_from_manifest(
            manifest,
            engine,
            event_bus=bus,
            environment="test",
        )
        assert report.total_wired == 1
        engine.freeze()

        scheduler = await start_lane_runtime_scheduler(bus, "main")
        assert scheduler is not None
        try:
            await scheduler.emit_tick()
            result = await asyncio.wait_for(observed.get(), timeout=5)
        finally:
            await scheduler.stop()

        assert result.payload.errors == []
        assert result.payload.repos_polled == [_REPO]
        assert result.payload.prs_polled == 1
    finally:
        await bus.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_bad_switch_on_a_shared_environment_stops_only_the_main_boot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every role of a lane reads one compose environment; only main owns the switch.

    The roles resolve their identity the way the kernel does
    (``resolve_runtime_profile_name()`` over ``RUNTIME_PROFILE``) against one
    started bus and one environment carrying an unparseable switch value. The
    secondary roles start no producer and publish nothing; main refuses its
    boot, which is the role the switch is for (OMN-20590 review follow-up).
    """
    monkeypatch.setenv("ONEX_RUNTIME_SCHEDULER_ENABLED", "ture")
    monkeypatch.setenv("ONEX_RUNTIME_SCHEDULER_PERSIST_SEQUENCE", "false")
    monkeypatch.setenv("ONEX_RUNTIME_SCHEDULER_TICK_INTERVAL_MS", "60000")
    bus = EventBusInmemory(environment="test", group="runtime-tick-bad-switch")
    await bus.start()
    try:
        for role in ("effects", "workers", "projection-api"):
            monkeypatch.setenv("RUNTIME_PROFILE", role)
            scheduler = await start_lane_runtime_scheduler(
                bus, resolve_runtime_profile_name()
            )
            assert scheduler is None, role

        monkeypatch.setenv("RUNTIME_PROFILE", "main")
        with pytest.raises(ProtocolConfigurationError, match="ture"):
            await start_lane_runtime_scheduler(bus, resolve_runtime_profile_name())

        assert await bus.get_event_history(topic=SUFFIX_RUNTIME_TICK) == []

        # Positive control: the same read sees a tick once main is asked for one.
        monkeypatch.setenv("ONEX_RUNTIME_SCHEDULER_ENABLED", "true")
        producer = await start_lane_runtime_scheduler(
            bus, resolve_runtime_profile_name()
        )
        assert producer is not None
        try:
            await producer.emit_tick()
        finally:
            await producer.stop()
        assert len(await bus.get_event_history(topic=SUFFIX_RUNTIME_TICK)) == 1
    finally:
        await bus.close()
