# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden-chain proof for the GitHub API poller runtime path.

OMN-11602 proves the transport-shaped path:
Kafka-compatible event bus -> auto-wired runtime tick subscription ->
MessageDispatchEngine -> HandlerGitHubApiPoll -> GitHub HTTP -> response topic.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.event_bus.models.model_event_message import ModelEventMessage
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
from omnimarket.nodes.node_github_pr_poller_effect.models.model_github_poller_result import (
    ModelGitHubPollerResult,
)

_INPUT_TOPIC = "onex.intent.platform.runtime-tick.v1"
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
                "number": 1709,
                "title": "Register GitHub API poller",
                "draft": False,
                "labels": [],
                "updated_at": datetime.now(tz=UTC).isoformat().replace("+00:00", "Z"),
                "combined_status": "success",
                "review_states": ["APPROVED"],
            }
        ]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_github_api_poll_golden_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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

    bus = EventBusInmemory(environment="test", group="github-api-poll-golden-chain")
    await bus.start()
    try:
        observed_results: asyncio.Queue[ModelGitHubPollerResult] = asyncio.Queue()
        correlation_id = UUID("44444444-4444-4444-8444-444444444444")

        async def collect_result(message: ModelEventMessage) -> None:
            envelope = ModelEventEnvelope[ModelGitHubPollerResult].model_validate_json(
                message.value
            )
            if envelope.correlation_id == correlation_id:
                await observed_results.put(envelope.payload)

        await bus.subscribe(
            topic=_OUTPUT_TOPIC,
            group_id="github-api-poll-result-collector",
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

        now = datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC)
        tick = ModelRuntimeTick(
            now=now,
            tick_id=UUID("55555555-5555-4555-8555-555555555555"),
            sequence_number=1,
            scheduled_at=now,
            correlation_id=correlation_id,
            scheduler_id="github-api-poll-golden-chain",
            tick_interval_ms=1000,
        )
        envelope = ModelEventEnvelope[object](
            payload=tick.model_dump(mode="json"),
            correlation_id=correlation_id,
            source_tool="github-api-poll-golden-chain",
        )

        await bus.publish(
            _INPUT_TOPIC,
            None,
            envelope.model_dump_json().encode("utf-8"),
            None,
        )

        result = await asyncio.wait_for(observed_results.get(), timeout=2)

        assert result.errors == []
        assert result.repos_polled == [_REPO]
        assert result.prs_polled == 1
        assert result.events_published == 0
        assert result.pending_events == [
            {
                "event_type": _OUTPUT_TOPIC,
                "repo": _REPO,
                "pr_number": 1709,
                "triage_state": "ready_to_merge",
                "title": "Register GitHub API poller",
                "is_draft": False,
                "partition_key": f"{_REPO}:1709",
            }
        ]
    finally:
        await bus.close()
