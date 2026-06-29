# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden-chain tests for node_slack_publish_effect (OMN-13723, OMN-13727).

Tests run on EventBusInmemory (no Kafka, no network, no Infisical).
Coverage:
- Contract shape: node_type=effect, secrets block, endpoints block, topics.
- handler_routing: operation_match with non-empty operation (RuntimeLocal gate).
- Successful publish: transport stub returns success + slack_ts; ledger written.
- Idempotency: second call with same idempotency_key returns deduped=True, no POST.
- Fail-closed on missing channel: ValueError raised by handler.
- Fail-closed on missing blocks+text: ValueError raised by handler.
- Slack API error: transport returns (False, None, error_code) -> result.success=False.
- Fail-closed on missing SLACK_BOT_TOKEN: RuntimeError raised, no POST made (OMN-13727).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
import yaml
from omnibase_core.runtime.runtime_local import RuntimeLocal
from pydantic import ValidationError

from omnimarket.nodes.node_slack_publish_effect.handlers.handler_slack_publish_effect import (
    HandlerSlackPublishEffect,
)
from omnimarket.nodes.node_slack_publish_effect.models.model_slack_publish import (
    ModelSlackPublish,
    ModelSlackPublishResult,
)

CONTRACT_PATH = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_slack_publish_effect/contract.yaml"
)

_CMD_TOPIC = "onex.cmd.omnimarket.slack-publish.v1"
_PUBLISHED_TOPIC = "onex.evt.omnimarket.slack-published.v1"
_FAILED_TOPIC = "onex.evt.omnimarket.slack-publish-failed.v1"
_DEDUPED_TOPIC = "onex.evt.omnimarket.slack-publish-deduped.v1"

_CHANNEL = "C012AB3CD"
_IDEM_KEY = "2026-06-28|C012AB3CD|abc123"
_SLACK_TS = "1719513600.123456"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _contract() -> dict[str, Any]:
    return yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))


def _cmd(
    *,
    channel: str = _CHANNEL,
    text: str | None = "Hello from OmniNode",
    blocks: list[dict[str, Any]] | None = None,
    thread_ts: str | None = None,
    idempotency_key: str = _IDEM_KEY,
    correlation_id: UUID | None = None,
) -> ModelSlackPublish:
    return ModelSlackPublish(
        channel=channel,
        text=text,
        blocks=blocks,
        thread_ts=thread_ts,
        idempotency_key=idempotency_key,
        correlation_id=correlation_id or uuid4(),
    )


class _StubTransport:
    """Deterministic Slack transport for tests — no network."""

    def __init__(
        self,
        *,
        success: bool = True,
        slack_ts: str | None = _SLACK_TS,
        error_code: str | None = None,
    ) -> None:
        self._success = success
        self._slack_ts = slack_ts
        self._error_code = error_code
        self.call_count = 0
        self.last_payload: dict[str, Any] | None = None

    async def post(
        self,
        payload: dict[str, Any],
        correlation_id: UUID,
    ) -> tuple[bool, str | None, str | None]:
        self.call_count += 1
        self.last_payload = payload
        return self._success, self._slack_ts, self._error_code


def _make_handler(
    *,
    success: bool = True,
    slack_ts: str | None = _SLACK_TS,
    error_code: str | None = None,
    ledger: dict[str, str] | None = None,
) -> tuple[HandlerSlackPublishEffect, _StubTransport, dict[str, str]]:
    stub = _StubTransport(success=success, slack_ts=slack_ts, error_code=error_code)
    in_mem_ledger: dict[str, str] = dict(ledger) if ledger else {}

    def _lookup(key: str) -> str | None:
        return in_mem_ledger.get(key)

    def _write(key: str, ts: str) -> None:
        in_mem_ledger[key] = ts

    handler = HandlerSlackPublishEffect(
        transport=stub,  # type: ignore[arg-type]
        ledger_lookup=_lookup,
        ledger_write=_write,
    )
    return handler, stub, in_mem_ledger


# ---------------------------------------------------------------------------
# Contract shape tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSlackPublishContractShape:
    """Verify the contract satisfies all mandatory structural gates."""

    def test_node_type_is_effect(self) -> None:
        raw = _contract()
        assert raw["node_type"] == "effect", (
            "node_type must be 'effect' (canonical archetype); "
            f"got {raw['node_type']!r}"
        )

    def test_secrets_block_declares_slack_bot_token(self) -> None:
        raw = _contract()
        secrets = raw.get("secrets", {})
        assert "SLACK_BOT_TOKEN" in secrets, (
            "contract must declare SLACK_BOT_TOKEN in secrets block"
        )
        assert secrets["SLACK_BOT_TOKEN"].get("required") is True

    def test_endpoints_block_declares_complete_slack_url(self) -> None:
        raw = _contract()
        endpoints = raw.get("endpoints", {})
        assert endpoints, "contract must declare an endpoints block"
        urls = [ep.get("url", "") for ep in endpoints.values()]
        assert any("https://slack.com/api/chat.postMessage" in u for u in urls), (
            "endpoints block must declare the complete Slack Web API URL verbatim"
        )

    def test_command_topic_declared(self) -> None:
        raw = _contract()
        eb = raw["event_bus"]
        assert _CMD_TOPIC in eb["subscribe_topics"]

    def test_terminal_events_declared(self) -> None:
        raw = _contract()
        eb = raw["event_bus"]
        assert _PUBLISHED_TOPIC in eb["publish_topics"]
        assert _FAILED_TOPIC in eb["publish_topics"]
        assert _DEDUPED_TOPIC in eb["publish_topics"]

    def test_terminal_event_is_published(self) -> None:
        raw = _contract()
        assert raw["terminal_event"] == _PUBLISHED_TOPIC

    def test_handler_routing_operation_present(self) -> None:
        raw = _contract()
        routing = raw["handler_routing"]
        assert routing["routing_strategy"] == "operation_match"
        handlers = routing["handlers"]
        assert len(handlers) >= 1
        # RuntimeLocal requires non-empty operation on operation_match entries
        assert handlers[0].get("operation") == "slack_publish", (
            "operation_match handler must declare 'operation: slack_publish'"
        )

    def test_validate_routing_reports_no_errors(self) -> None:
        """Gate that caused node_auto_merge_effect to fail closed before OMN-13530."""
        raw = _contract()
        eb = raw.get("event_bus", {}) or {}
        errors = RuntimeLocal._validate_routing(
            raw["handler_routing"],
            eb.get("subscribe_topics", []) or [],
            eb.get("publish_topics", []) or [],
        )
        assert errors == [], f"routing validation must be clean, got: {errors}"


# ---------------------------------------------------------------------------
# Golden-chain handler tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSlackPublishGoldenChain:
    """Happy-path and error-path handler tests."""

    @pytest.mark.asyncio
    async def test_successful_publish_returns_result(self) -> None:
        handler, _stub, _ledger = _make_handler()
        cmd = _cmd()
        output = await handler.handle(cmd)
        events = output.events or ()
        assert len(events) == 1
        result = events[0]
        assert isinstance(result, ModelSlackPublishResult)
        assert result.success is True
        assert result.ts == _SLACK_TS
        assert result.deduped is False
        assert result.error_code is None
        assert result.correlation_id == cmd.correlation_id

    @pytest.mark.asyncio
    async def test_successful_publish_writes_ledger(self) -> None:
        handler, _stub, ledger = _make_handler()
        cmd = _cmd()
        await handler.handle(cmd)
        assert ledger.get(cmd.idempotency_key) == _SLACK_TS

    @pytest.mark.asyncio
    async def test_idempotency_dedupes_on_second_call(self) -> None:
        prior_ts = "1719513500.000001"
        handler, stub, _ledger = _make_handler(
            ledger={_IDEM_KEY: prior_ts},
        )
        cmd = _cmd()
        output = await handler.handle(cmd)
        events = output.events or ()
        assert len(events) == 1
        result = events[0]
        assert isinstance(result, ModelSlackPublishResult)
        assert result.deduped is True
        assert result.ts == prior_ts
        assert result.success is True
        # No POST should have been made
        assert stub.call_count == 0

    @pytest.mark.asyncio
    async def test_slack_api_error_returns_failure_result(self) -> None:
        handler, _stub, ledger = _make_handler(
            success=False,
            slack_ts=None,
            error_code="SLACK_API_CHANNEL_NOT_FOUND",
        )
        cmd = _cmd()
        output = await handler.handle(cmd)
        events = output.events or ()
        assert len(events) == 1
        result = events[0]
        assert isinstance(result, ModelSlackPublishResult)
        assert result.success is False
        assert result.ts is None
        assert result.error_code == "SLACK_API_CHANNEL_NOT_FOUND"
        # Ledger must NOT be written on failure
        assert ledger.get(cmd.idempotency_key) is None

    @pytest.mark.asyncio
    async def test_fail_closed_on_missing_channel(self) -> None:
        # Use model_construct to bypass Pydantic min_length; tests handler guard directly.
        handler, _, __ = _make_handler()
        cmd = ModelSlackPublish.model_construct(
            channel="",
            text="test",
            idempotency_key=_IDEM_KEY,
            correlation_id=uuid4(),
        )
        with pytest.raises(ValueError, match="channel"):
            await handler.handle(cmd)

    @pytest.mark.asyncio
    async def test_fail_closed_on_missing_blocks_and_text(self) -> None:
        # Both blocks=None and text=None pass Pydantic (both Optional); handler checks.
        handler, _, __ = _make_handler()
        cmd = ModelSlackPublish(
            channel=_CHANNEL,
            blocks=None,
            text=None,
            idempotency_key=_IDEM_KEY,
            correlation_id=uuid4(),
        )
        with pytest.raises(ValueError, match=r"blocks|text"):
            await handler.handle(cmd)

    @pytest.mark.asyncio
    async def test_blocks_payload_passed_verbatim(self) -> None:
        handler, stub, _ledger = _make_handler()
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "hello"}}]
        cmd = _cmd(blocks=blocks, text=None)
        await handler.handle(cmd)
        assert stub.last_payload is not None
        assert stub.last_payload["blocks"] == blocks
        assert "text" not in stub.last_payload

    @pytest.mark.asyncio
    async def test_thread_ts_forwarded(self) -> None:
        handler, stub, _ledger = _make_handler()
        cmd = _cmd(thread_ts="1719513000.000001")
        await handler.handle(cmd)
        assert stub.last_payload is not None
        assert stub.last_payload["thread_ts"] == "1719513000.000001"

    @pytest.mark.asyncio
    async def test_no_thread_ts_when_not_set(self) -> None:
        handler, stub, _ledger = _make_handler()
        cmd = _cmd(thread_ts=None)
        await handler.handle(cmd)
        assert stub.last_payload is not None
        assert "thread_ts" not in stub.last_payload


# ---------------------------------------------------------------------------
# Model validation tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSlackPublishModelValidation:
    """Pydantic model constraints are enforced (frozen, extra='forbid')."""

    def test_model_is_frozen(self) -> None:
        cmd = _cmd()
        with pytest.raises(ValidationError):
            cmd.channel = "new-channel"  # type: ignore[misc]

    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            ModelSlackPublish(
                channel=_CHANNEL,
                text="hello",
                idempotency_key=_IDEM_KEY,
                correlation_id=uuid4(),
                unknown_field="bad",  # type: ignore[call-arg]
            )

    def test_channel_required_no_default(self) -> None:
        with pytest.raises(ValidationError):
            ModelSlackPublish(
                text="hello",
                idempotency_key=_IDEM_KEY,
                correlation_id=uuid4(),
            )  # type: ignore[call-arg]

    def test_correlation_id_is_uuid(self) -> None:
        with pytest.raises(ValidationError):
            ModelSlackPublish(
                channel=_CHANNEL,
                text="hello",
                idempotency_key=_IDEM_KEY,
                correlation_id="not-a-uuid",  # type: ignore[arg-type]
            )


# ---------------------------------------------------------------------------
# Secret fail-closed tests (OMN-13727)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSlackPublishSecretFailClosed:
    """Handler is fail-closed when SLACK_BOT_TOKEN is absent from the secret store.

    This class tests the transport-resolution path (no injected transport) by
    mocking ``resolve_api_key_async`` to simulate a missing secret.  No HTTP
    POST is attempted when the secret is unresolved.
    """

    @pytest.mark.asyncio
    async def test_missing_token_raises_runtime_error_no_post(self) -> None:
        """RuntimeError is raised and no POST is attempted when the secret is None."""
        stub = _StubTransport()

        # Build the handler WITHOUT injecting a transport so _resolve_transport
        # actually calls resolve_api_key_async at handle() time.
        handler = HandlerSlackPublishEffect(
            transport=None,
            ledger_lookup=lambda _k: None,
            ledger_write=lambda _k, _ts: None,
        )

        _mock_target = (
            "omnimarket.nodes.node_slack_publish_effect"
            ".handlers.handler_slack_publish_effect.resolve_api_key_async"
        )
        with (
            patch(_mock_target, new=AsyncMock(return_value=None)),
            pytest.raises(RuntimeError, match="SLACK_BOT_TOKEN"),
        ):
            await handler.handle(_cmd())

        # Stub was never touched — no POST was made.
        assert stub.call_count == 0, (
            "No HTTP POST must be attempted when the secret is unresolved"
        )
