# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for overseer verifier correlated wait in HandlerBuildLoopOrchestrator.

Covers: PASS, FAIL, timeout paths for the VERIFYING phase — OMN-8151.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from omnimarket.nodes.node_build_loop_orchestrator.handlers.handler_build_loop_orchestrator import (
    _VERIFIER_TIMEOUT_SECONDS,
    HandlerBuildLoopOrchestrator,
)

TOPIC_OVERSEER_VERIFICATION_COMPLETED = (
    "onex.evt.omnimarket.overseer-verifier-completed.v1"
)
TOPIC_OVERSEER_VERIFY_REQUESTED = "onex.cmd.omnimarket.overseer-verify.v1"


class _FakeMessage:
    """Minimal stand-in for ModelEventMessage with a .value bytes field."""

    def __init__(self, value: bytes) -> None:
        self.value = value


_AsyncCallback = Callable[[_FakeMessage], Awaitable[None]]


class _FakeEventBus:
    """In-memory event bus stub for testing the correlated wait pattern."""

    def __init__(self) -> None:
        self.published: list[tuple[str, bytes]] = []
        self._callbacks: dict[str, list[_AsyncCallback]] = {}

    async def publish(self, *, topic: str, key: object, value: bytes) -> None:
        self.published.append((topic, value))

    async def subscribe(
        self,
        topic: str,
        node_identity: object = None,
        on_message: _AsyncCallback | None = None,
        **kwargs: Any,
    ) -> None:
        if on_message is not None:
            self._callbacks.setdefault(topic, []).append(on_message)

    async def deliver(self, topic: str, payload: bytes) -> None:
        msg = _FakeMessage(payload)
        for cb in self._callbacks.get(topic, []):
            await cb(msg)


def _make_orchestrator(event_bus: _FakeEventBus) -> HandlerBuildLoopOrchestrator:
    return HandlerBuildLoopOrchestrator(event_bus=event_bus)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# _run_overseer_verify: PASS path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verifier_pass_path() -> None:
    """Overseer returns passed=True → success."""
    bus = _FakeEventBus()
    orch = _make_orchestrator(bus)
    correlation_id = uuid4()

    async def simulate_pass() -> None:
        await asyncio.sleep(0)
        verdict = json.dumps(
            {"correlation_id": str(correlation_id), "passed": True}
        ).encode()
        await bus.deliver(TOPIC_OVERSEER_VERIFICATION_COMPLETED, verdict)

    _task = asyncio.ensure_future(simulate_pass())
    success, error, _metrics = await orch._run_overseer_verify(  # noqa: SLF001
        correlation_id=correlation_id, dry_run=False
    )

    assert success is True
    assert error is None
    topics_published = [t for t, _ in bus.published]
    assert TOPIC_OVERSEER_VERIFY_REQUESTED in topics_published
    _ = _task  # suppress unused-variable


# ---------------------------------------------------------------------------
# _run_overseer_verify: FAIL path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verifier_fail_path() -> None:
    """Overseer returns passed=False → FAILED with failed_criteria reason."""
    bus = _FakeEventBus()
    orch = _make_orchestrator(bus)
    correlation_id = uuid4()

    async def simulate_fail() -> None:
        await asyncio.sleep(0)
        verdict = json.dumps(
            {
                "correlation_id": str(correlation_id),
                "passed": False,
                "failed_criteria": ["coverage < 80%", "missing contract"],
            }
        ).encode()
        await bus.deliver(TOPIC_OVERSEER_VERIFICATION_COMPLETED, verdict)

    _task = asyncio.ensure_future(simulate_fail())
    success, error, _ = await orch._run_overseer_verify(  # noqa: SLF001
        correlation_id=correlation_id, dry_run=False
    )

    assert success is False
    assert error is not None
    assert "coverage" in error or "contract" in error
    _ = _task


# ---------------------------------------------------------------------------
# _run_overseer_verify: timeout path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verifier_timeout_path() -> None:
    """No verdict received within timeout → FAILED with verifier_timeout reason."""
    bus = _FakeEventBus()
    orch = _make_orchestrator(bus)
    correlation_id = uuid4()

    with patch(
        "omnimarket.nodes.node_build_loop_orchestrator.handlers.handler_build_loop_orchestrator._VERIFIER_TIMEOUT_SECONDS",
        0.05,
    ):
        success, error, _ = await orch._run_overseer_verify(  # noqa: SLF001
            correlation_id=correlation_id, dry_run=False
        )

    assert success is False
    assert error == "verifier_timeout"


# ---------------------------------------------------------------------------
# _run_overseer_verify: dry_run falls back to legacy verify
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verifier_dry_run_uses_legacy_verify() -> None:
    """dry_run=True falls back to legacy ProtocolVerifyHandler, no Kafka publish."""
    bus = _FakeEventBus()

    mock_verify = AsyncMock()
    mock_result = MagicMock()
    mock_result.all_critical_passed = True
    mock_verify.handle = AsyncMock(return_value=mock_result)

    orch = _make_orchestrator(bus)
    orch._verify = mock_verify  # noqa: SLF001
    correlation_id = uuid4()

    success, error, _ = await orch._run_overseer_verify(  # noqa: SLF001
        correlation_id=correlation_id, dry_run=True
    )

    assert success is True
    assert error is None
    assert len(bus.published) == 0
    mock_verify.handle.assert_called_once()


# ---------------------------------------------------------------------------
# _run_overseer_verify: no event bus falls back to legacy verify
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verifier_no_event_bus_uses_legacy_verify() -> None:
    """No event_bus → falls back to legacy ProtocolVerifyHandler."""
    mock_verify = AsyncMock()
    mock_result = MagicMock()
    mock_result.all_critical_passed = True
    mock_verify.handle = AsyncMock(return_value=mock_result)

    orch = HandlerBuildLoopOrchestrator()
    orch._verify = mock_verify  # noqa: SLF001
    orch._event_bus = None  # noqa: SLF001
    correlation_id = uuid4()

    success, error, _ = await orch._run_overseer_verify(  # noqa: SLF001
        correlation_id=correlation_id, dry_run=False
    )

    assert success is True
    assert error is None
    mock_verify.handle.assert_called_once()


# ---------------------------------------------------------------------------
# _run_overseer_verify: late/duplicate events ignored after verdict
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_late_event_ignored_after_verdict() -> None:
    """Late duplicate events after verdict is already set are ignored."""
    bus = _FakeEventBus()
    orch = _make_orchestrator(bus)
    correlation_id = uuid4()

    async def simulate_pass_then_late() -> None:
        await asyncio.sleep(0)
        verdict = json.dumps(
            {"correlation_id": str(correlation_id), "passed": True}
        ).encode()
        await bus.deliver(TOPIC_OVERSEER_VERIFICATION_COMPLETED, verdict)
        await asyncio.sleep(0.01)
        late = json.dumps(
            {"correlation_id": str(correlation_id), "passed": False}
        ).encode()
        await bus.deliver(TOPIC_OVERSEER_VERIFICATION_COMPLETED, late)

    _task = asyncio.ensure_future(simulate_pass_then_late())
    success, error, _ = await orch._run_overseer_verify(  # noqa: SLF001
        correlation_id=correlation_id, dry_run=False
    )

    assert success is True
    assert error is None
    _ = _task


# ---------------------------------------------------------------------------
# topics constant
# ---------------------------------------------------------------------------


def test_verifier_timeout_default() -> None:
    """Default timeout is 120 seconds."""
    assert _VERIFIER_TIMEOUT_SECONDS == 120
