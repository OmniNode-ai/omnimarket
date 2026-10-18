# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Only PR status events reach onex.evt.github.pr-status.v1.

Once the dev lane's main runtime published the runtime tick, every tick reached
``HandlerGitHubApiPoll`` and the runtime published the handler's poll-cycle
summary (``{"events_published": 0, "repos_polled": [], "prs_polled": 0,
"errors": [], "pending_events": []}``) onto the node's publish topic, once per
tick. Every consumer of that topic refused it: the PR-state projection for a
missing ``repo`` and ``branch_claim_check_effect`` by routing 100% of what it
consumed to its DLQ, which held the dev runtime's health probe DEGRADED.

The chain is the one the kernel wires: contract manifest -> ``wire_from_manifest``
-> runtime-tick subscription -> ``HandlerGitHubApiPoll`` -> result applier ->
event bus.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.runtime.auto_wiring import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import wire_from_manifest
from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.nodes.node_github_pr_poller_effect.handlers import (
    handler_github_api_poll,
)
from omnimarket.nodes.node_github_pr_poller_effect.models.model_github_poller_config import (
    ModelGitHubPollerConfig,
)

_INPUT_TOPIC = "onex.intent.platform.runtime-tick.v1"
_OUTPUT_TOPIC = "onex.evt.github.pr-status.v1"
_REPO = "OmniNode-ai/omnibase_infra"
_POLL_SUMMARY_KEYS = frozenset(
    {"events_published", "repos_polled", "prs_polled", "errors", "pending_events"}
)


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
    calls = 0

    def __init__(self, _token: str) -> None:
        pass

    def fetch_open_prs_for_triage(self, repo: str) -> list[dict[str, object]]:
        assert repo == _REPO
        type(self).calls += 1
        return [
            {
                "number": 20867,
                "title": "Publish only PR status events",
                "draft": False,
                "labels": [],
                "updated_at": datetime.now(tz=UTC).isoformat().replace("+00:00", "Z"),
                "combined_status": "success",
                "review_states": ["APPROVED"],
            },
            {
                "number": 20868,
                "title": "A draft",
                "draft": True,
                "labels": [],
                "updated_at": datetime.now(tz=UTC).isoformat().replace("+00:00", "Z"),
                "combined_status": "pending",
                "review_states": [],
            },
        ]


def _tick_bytes(sequence_number: int) -> bytes:
    now = datetime(2026, 10, 11, 3, 20, 0, tzinfo=UTC)
    correlation_id = UUID(f"20867000-0000-4000-8000-{sequence_number:012d}")
    tick = ModelRuntimeTick(
        now=now,
        tick_id=UUID(f"20867111-0000-4000-8000-{sequence_number:012d}"),
        sequence_number=sequence_number,
        scheduled_at=now,
        correlation_id=correlation_id,
        scheduler_id="github-poller-publish",
        tick_interval_ms=1000,
    )
    envelope = ModelEventEnvelope[object](
        payload=tick.model_dump(mode="json"),
        correlation_id=correlation_id,
        source_tool="github-poller-publish",
    )
    return envelope.model_dump_json().encode("utf-8")


async def _wired_bus(
    monkeypatch: pytest.MonkeyPatch, config: ModelGitHubPollerConfig
) -> tuple[EventBusInmemory, list[ModelHandlerOutput[None]]]:
    """Wire the poller contract as the kernel does; record every handler call."""

    async def token() -> str:
        return "test-token"

    monkeypatch.setattr(handler_github_api_poll, "_resolve_github_token", token)
    monkeypatch.setattr(
        handler_github_api_poll, "_load_contract_config", lambda: config
    )
    monkeypatch.setattr(
        handler_github_api_poll, "GitHubHttpTransport", _FakeGitHubClient
    )
    _FakeGitHubClient.calls = 0

    handled: list[ModelHandlerOutput[None]] = []
    real_handle = handler_github_api_poll.HandlerGitHubApiPoll.handle

    async def recording_handle(
        self: handler_github_api_poll.HandlerGitHubApiPoll, request: ModelRuntimeTick
    ) -> ModelHandlerOutput[None]:
        result = await real_handle(self, request)
        handled.append(result)
        return result

    monkeypatch.setattr(
        handler_github_api_poll.HandlerGitHubApiPoll, "handle", recording_handle
    )

    manifest = discover_contracts_from_paths([_contract_path()])
    assert manifest.total_errors == 0
    bus = EventBusInmemory(environment="test", group="github-poller-publish")
    await bus.start()
    engine = MessageDispatchEngine()
    report = await wire_from_manifest(
        manifest, engine, event_bus=bus, environment="test"
    )
    assert report.total_wired == 1
    engine.freeze()
    return bus, handled


async def _published_payloads(bus: EventBusInmemory) -> list[dict[str, object]]:
    history = await bus.get_event_history(topic=_OUTPUT_TOPIC)
    payloads: list[dict[str, object]] = []
    for message in history:
        payload = json.loads(message.value)["payload"]
        assert isinstance(payload, dict)
        payloads.append(payload)
    return payloads


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unconfigured_poller_publishes_nothing_on_a_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dev lane shape: repos=[] (the contract default), one tick a second."""
    bus, handled = await _wired_bus(monkeypatch, ModelGitHubPollerConfig(repos=[]))
    try:
        for sequence_number in (1, 2, 3):
            await bus.publish(_INPUT_TOPIC, None, _tick_bytes(sequence_number), None)

        # Positive control: every tick reached the handler.
        assert len(handled) == 3
        assert _FakeGitHubClient.calls == 0
        assert await _published_payloads(bus) == []
    finally:
        await bus.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_configured_poller_publishes_one_status_event_per_pr_and_no_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bus, handled = await _wired_bus(
        monkeypatch,
        ModelGitHubPollerConfig(repos=[_REPO], poll_interval_seconds=60),
    )
    try:
        await bus.publish(_INPUT_TOPIC, None, _tick_bytes(1), None)
        payloads = await _published_payloads(bus)

        assert len(handled) == 1
        assert _FakeGitHubClient.calls == 1
        assert [p.get("pr_number") for p in payloads] == [20867, 20868]
        for payload in payloads:
            assert not _POLL_SUMMARY_KEYS & payload.keys(), payload
            assert payload["repo"] == _REPO
            assert payload["event_type"] == _OUTPUT_TOPIC
            assert payload["partition_key"] == f"{_REPO}:{payload['pr_number']}"
        assert [p["triage_state"] for p in payloads] == ["ready_to_merge", "draft"]
        assert [p["is_draft"] for p in payloads] == [False, True]
        assert [p["title"] for p in payloads] == [
            "Publish only PR status events",
            "A draft",
        ]

        # A tick inside poll_interval_seconds polls nothing and publishes nothing.
        await bus.publish(_INPUT_TOPIC, None, _tick_bytes(2), None)
        assert len(handled) == 2
        assert _FakeGitHubClient.calls == 1
        assert len(await _published_payloads(bus)) == 2
    finally:
        await bus.close()
