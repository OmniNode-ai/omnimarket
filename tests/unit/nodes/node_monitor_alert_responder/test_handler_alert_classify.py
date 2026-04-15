# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for HandlerAlertClassify — OMN-8886.

Tests assert observable tier + notes output for each classification path.
No external deps; handler is pure Python.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from omnimarket.nodes.node_monitor_alert_responder.handlers.handler_alert_classify import (
    HandlerAlertClassify,
)
from omnimarket.nodes.node_monitor_alert_responder.models.model_alert_event import (
    ModelAlertEvent,
)
from omnimarket.nodes.node_monitor_alert_responder.models.model_alert_response import (
    ModelAlertResponse,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_event(**kwargs: object) -> ModelAlertEvent:
    defaults: dict[str, object] = {
        "alert_id": "test-alert-001",
        "source": "omninode-runtime",
        "severity": "ERROR",
        "pattern_matched": "generic_error",
        "container": "omninode-runtime",
        "full_message_text": "Something went wrong",
        "detected_at": "2026-04-15T12:00:00+00:00",
        "host": "omni-host",
    }
    defaults.update(kwargs)
    return ModelAlertEvent(**defaults)  # type: ignore[arg-type]


@pytest.fixture
def handler() -> HandlerAlertClassify:
    return HandlerAlertClassify()


# ---------------------------------------------------------------------------
# Tier: ESCALATE (CRITICAL severity)
# ---------------------------------------------------------------------------


class TestEscalateCriticalSeverity:
    @pytest.mark.asyncio
    async def test_critical_severity_returns_escalate(
        self, handler: HandlerAlertClassify
    ) -> None:
        event = _make_event(severity="CRITICAL", pattern_matched="disk_full")
        result = await handler.handle(uuid4(), event)
        assert isinstance(result, ModelAlertResponse)
        assert result.tier == "ESCALATE"
        assert result.alert_id == "test-alert-001"

    @pytest.mark.asyncio
    async def test_critical_overrides_recoverable_pattern(
        self, handler: HandlerAlertClassify
    ) -> None:
        """CRITICAL + memory pattern still escalates — severity wins."""
        event = _make_event(severity="CRITICAL", pattern_matched="oom_killer")
        result = await handler.handle(uuid4(), event)
        assert result.tier == "ESCALATE"

    @pytest.mark.asyncio
    async def test_escalate_notes_mention_severity(
        self, handler: HandlerAlertClassify
    ) -> None:
        event = _make_event(severity="CRITICAL")
        result = await handler.handle(uuid4(), event)
        assert "CRITICAL" in result.notes


# ---------------------------------------------------------------------------
# Tier: RECOVERABLE (known pattern keywords)
# ---------------------------------------------------------------------------


class TestRecoverablePatterns:
    @pytest.mark.asyncio
    async def test_oom_pattern_is_recoverable(
        self, handler: HandlerAlertClassify
    ) -> None:
        event = _make_event(pattern_matched="oom_killer_invoked")
        result = await handler.handle(uuid4(), event)
        assert result.tier == "RECOVERABLE"

    @pytest.mark.asyncio
    async def test_memory_pattern_is_recoverable(
        self, handler: HandlerAlertClassify
    ) -> None:
        event = _make_event(pattern_matched="high_memory_usage")
        result = await handler.handle(uuid4(), event)
        assert result.tier == "RECOVERABLE"

    @pytest.mark.asyncio
    async def test_restart_pattern_is_recoverable(
        self, handler: HandlerAlertClassify
    ) -> None:
        event = _make_event(
            pattern_matched="container_restart_detected", restart_count=2
        )
        result = await handler.handle(uuid4(), event)
        assert result.tier == "RECOVERABLE"

    @pytest.mark.asyncio
    async def test_timeout_pattern_is_recoverable(
        self, handler: HandlerAlertClassify
    ) -> None:
        event = _make_event(pattern_matched="kafka_connection_timeout")
        result = await handler.handle(uuid4(), event)
        assert result.tier == "RECOVERABLE"

    @pytest.mark.asyncio
    async def test_disk_full_pattern_is_recoverable(
        self, handler: HandlerAlertClassify
    ) -> None:
        event = _make_event(pattern_matched="disk_full_alert")
        result = await handler.handle(uuid4(), event)
        assert result.tier == "RECOVERABLE"

    @pytest.mark.asyncio
    async def test_recoverable_echoes_alert_id(
        self, handler: HandlerAlertClassify
    ) -> None:
        event = _make_event(alert_id="abc-123", pattern_matched="connection_refused")
        result = await handler.handle(uuid4(), event)
        assert result.tier == "RECOVERABLE"
        assert result.alert_id == "abc-123"


# ---------------------------------------------------------------------------
# Tier: ESCALATE (restart storm)
# ---------------------------------------------------------------------------


class TestEscalateRestartStorm:
    @pytest.mark.asyncio
    async def test_high_restart_count_escalates(
        self, handler: HandlerAlertClassify
    ) -> None:
        event = _make_event(pattern_matched="unknown_error", restart_count=5)
        result = await handler.handle(uuid4(), event)
        assert result.tier == "ESCALATE"

    @pytest.mark.asyncio
    async def test_restart_count_below_threshold_does_not_escalate(
        self, handler: HandlerAlertClassify
    ) -> None:
        event = _make_event(pattern_matched="unknown_error", restart_count=4)
        result = await handler.handle(uuid4(), event)
        assert result.tier == "UNKNOWN"

    @pytest.mark.asyncio
    async def test_restart_storm_notes_mention_count(
        self, handler: HandlerAlertClassify
    ) -> None:
        event = _make_event(pattern_matched="unknown_error", restart_count=10)
        result = await handler.handle(uuid4(), event)
        assert result.tier == "ESCALATE"
        assert "restart" in result.notes.lower()


# ---------------------------------------------------------------------------
# Tier: UNKNOWN
# ---------------------------------------------------------------------------


class TestUnknownTier:
    @pytest.mark.asyncio
    async def test_unknown_pattern_no_restarts(
        self, handler: HandlerAlertClassify
    ) -> None:
        event = _make_event(pattern_matched="exotic_db_corruption")
        result = await handler.handle(uuid4(), event)
        assert result.tier == "UNKNOWN"

    @pytest.mark.asyncio
    async def test_unknown_tier_no_playbook_id(
        self, handler: HandlerAlertClassify
    ) -> None:
        event = _make_event(pattern_matched="exotic_db_corruption")
        result = await handler.handle(uuid4(), event)
        assert result.playbook_id is None
        assert result.linear_ticket_id is None


# ---------------------------------------------------------------------------
# Handler metadata properties
# ---------------------------------------------------------------------------


class TestHandlerMetadata:
    def test_handler_type(self, handler: HandlerAlertClassify) -> None:
        assert handler.handler_type == "NODE_HANDLER"

    def test_handler_category(self, handler: HandlerAlertClassify) -> None:
        assert handler.handler_category == "EFFECT"
