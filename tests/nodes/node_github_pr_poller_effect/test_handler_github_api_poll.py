# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for HandlerGitHubApiPoll adapter wiring."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from omnibase_core.enums import EnumNodeKind
from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.events.topics import GITHUB_PR_STATUS_TOPIC_V1
from omnimarket.nodes.node_github_pr_poller_effect.handlers.handler_github_api_poll import (
    HandlerGitHubApiPoll,
)
from omnimarket.nodes.node_github_pr_poller_effect.models.model_github_poller_config import (
    ModelGitHubPollerConfig,
)
from omnimarket.nodes.node_github_pr_poller_effect.models.model_github_pr_status_poll_event import (
    ModelGitHubPrStatusPollEvent,
)

# The fields every ModelRuntimeEventBase stamps per instance (ids, clock).
_PER_INSTANCE_FIELDS = {"event_id", "correlation_id", "timestamp", "source_node_id"}


def _published(result: ModelHandlerOutput[None]) -> list[dict[str, object]]:
    """The wire payloads of the events the runtime will publish."""
    assert result.node_kind is EnumNodeKind.EFFECT
    assert result.result is None
    assert all(isinstance(e, ModelGitHubPrStatusPollEvent) for e in result.events)
    return [
        e.model_dump(mode="json", exclude=_PER_INSTANCE_FIELDS) for e in result.events
    ]


def _tick() -> ModelRuntimeTick:
    now = datetime.now(tz=UTC)
    return ModelRuntimeTick(
        now=now,
        tick_id=uuid4(),
        sequence_number=1,
        scheduled_at=now,
        correlation_id=uuid4(),
        scheduler_id="test-scheduler",
        tick_interval_ms=1000,
    )


class _FakeGitHubClient:
    def __init__(self, token: str) -> None:
        self.token = token
        self.repos: list[str] = []

    def fetch_open_prs_for_triage(self, repo: str) -> list[dict[str, object]]:
        self.repos.append(repo)
        return [
            {
                "number": 42,
                "title": "Ready PR",
                "draft": False,
                "labels": [],
                "updated_at": datetime.now(tz=UTC).isoformat(),
                "combined_status": "success",
                "review_states": ["APPROVED"],
            }
        ]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_handle_builds_the_client_from_the_token_and_polls_each_repo() -> None:
    created_clients: list[_FakeGitHubClient] = []

    def factory(token: str) -> _FakeGitHubClient:
        client = _FakeGitHubClient(token)
        created_clients.append(client)
        return client

    config = ModelGitHubPollerConfig(
        repos=["OmniNode-ai/omnimarket"],
        poll_interval_seconds=10,
        github_token_env_var="GITHUB_TOKEN",
    )
    handler = HandlerGitHubApiPoll(
        github_token="fake-token",
        github_client_factory=factory,
        config=config,
    )

    result = await handler.handle(_tick())

    assert result.logs == ()
    assert result.metrics["repos_polled"] == 1
    assert result.metrics["prs_polled"] == 1
    assert _published(result) == [
        {
            "event_type": GITHUB_PR_STATUS_TOPIC_V1,
            "repo": "OmniNode-ai/omnimarket",
            "pr_number": 42,
            "triage_state": "ready_to_merge",
            "title": "Ready PR",
            "is_draft": False,
            "partition_key": "OmniNode-ai/omnimarket:42",
        }
    ]
    assert created_clients[0].token == "fake-token"
    assert created_clients[0].repos == ["OmniNode-ai/omnimarket"]


class _FakeDraftGitHubClient(_FakeGitHubClient):
    """PR fixture with draft=True (OMN-14394 seam gap regression coverage)."""

    def fetch_open_prs_for_triage(self, repo: str) -> list[dict[str, object]]:
        self.repos.append(repo)
        return [
            {
                "number": 7,
                "title": "WIP PR",
                "draft": True,
                "labels": [],
                "updated_at": datetime.now(tz=UTC).isoformat(),
                "combined_status": "pending",
                "review_states": [],
            }
        ]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_handle_publishes_is_draft_true_for_draft_pr() -> None:
    """OMN-14394: is_draft must round-trip pr['draft'] onto the published event
    -- this is the field the pr_state projection (OMN-14375) and the
    OMN-14374 read skill's ModelOpenPrSummary.is_draft depend on."""

    def factory(token: str) -> _FakeDraftGitHubClient:
        return _FakeDraftGitHubClient(token)

    config = ModelGitHubPollerConfig(
        repos=["OmniNode-ai/omnimarket"],
        poll_interval_seconds=10,
    )
    handler = HandlerGitHubApiPoll(
        github_token="fake-token", github_client_factory=factory, config=config
    )

    result = await handler.handle(_tick())

    assert _published(result) == [
        {
            "event_type": GITHUB_PR_STATUS_TOPIC_V1,
            "repo": "OmniNode-ai/omnimarket",
            "pr_number": 7,
            "triage_state": "draft",
            "title": "WIP PR",
            "is_draft": True,
            "partition_key": "OmniNode-ai/omnimarket:7",
        }
    ]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_handle_surfaces_github_client_initialization_errors() -> None:
    def factory(_token: str) -> _FakeGitHubClient:
        raise RuntimeError("missing token")

    config = ModelGitHubPollerConfig(
        repos=["OmniNode-ai/omnimarket"],
        poll_interval_seconds=10,
    )
    handler = HandlerGitHubApiPoll(
        github_token="fake-token", github_client_factory=factory, config=config
    )

    result = await handler.handle(_tick())

    assert result.metrics["repos_polled"] == 0
    assert result.metrics["prs_polled"] == 0
    assert result.events == ()
    assert len(result.logs) == 1
    assert "Error initializing GitHub client: RuntimeError" in result.logs[0]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_handle_with_no_configured_repos_returns_no_events() -> None:
    """repos=[] (the contract default) returns nothing to publish."""
    created: list[object] = []

    def factory(token: str) -> _FakeGitHubClient:
        client = _FakeGitHubClient(token)
        created.append(client)
        return client

    handler = HandlerGitHubApiPoll(
        github_client_factory=factory, config=ModelGitHubPollerConfig(repos=[])
    )

    result = await handler.handle(_tick())

    assert result.events == ()
    assert result.result is None
    assert created == []


@pytest.mark.asyncio
@pytest.mark.unit
async def test_handle_inside_poll_interval_returns_no_events() -> None:
    """A second tick before poll_interval_seconds elapses polls and returns nothing."""
    created: list[_FakeGitHubClient] = []

    def factory(token: str) -> _FakeGitHubClient:
        client = _FakeGitHubClient(token)
        created.append(client)
        return client

    config = ModelGitHubPollerConfig(
        repos=["OmniNode-ai/omnimarket"], poll_interval_seconds=60
    )
    handler = HandlerGitHubApiPoll(
        github_token="fake-token", github_client_factory=factory, config=config
    )

    first = await handler.handle(_tick())
    second = await handler.handle(_tick())

    assert len(first.events) == 1
    assert second.events == ()
    assert [c.repos for c in created] == [["OmniNode-ai/omnimarket"], []]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_handle_resolves_the_token_from_the_contract_secret_when_none_is_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pydantic import SecretStr

    from omnimarket.nodes.node_github_pr_poller_effect.handlers import (
        handler_github_api_poll as module,
    )

    resolved: list[tuple[str, str | None]] = []

    async def fake_resolve(
        ref: str, *, env_var_fallback: str | None = None
    ) -> SecretStr:
        resolved.append((ref, env_var_fallback))
        return SecretStr("resolved-token")

    monkeypatch.setattr(module, "resolve_api_key_async", fake_resolve)
    created: list[_FakeGitHubClient] = []

    def factory(token: str) -> _FakeGitHubClient:
        client = _FakeGitHubClient(token)
        created.append(client)
        return client

    config = ModelGitHubPollerConfig(
        repos=["OmniNode-ai/omnimarket"], poll_interval_seconds=10
    )
    handler = HandlerGitHubApiPoll(github_client_factory=factory, config=config)

    result = await handler.handle(_tick())

    assert result.logs == ()
    assert resolved == [("GITHUB_TOKEN", "GITHUB_TOKEN")]
    assert created[0].token == "resolved-token"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_handle_with_no_repos_resolves_no_token_and_builds_no_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnimarket.nodes.node_github_pr_poller_effect.handlers import (
        handler_github_api_poll as module,
    )

    async def boom(*_a: object, **_k: object) -> object:
        raise AssertionError("no token resolution expected with no repos")

    monkeypatch.setattr(module, "resolve_api_key_async", boom)

    def factory(_token: str) -> _FakeGitHubClient:
        raise AssertionError("no client expected with no repos")

    handler = HandlerGitHubApiPoll(
        github_client_factory=factory, config=ModelGitHubPollerConfig(repos=[])
    )

    result = await handler.handle(_tick())

    assert result.events == ()
    assert result.result is None
    assert result.logs == ()
    assert result.metrics == {"repos_polled": 0.0, "prs_polled": 0.0, "events": 0.0}
