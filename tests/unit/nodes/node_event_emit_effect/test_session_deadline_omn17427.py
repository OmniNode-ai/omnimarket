# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A slow Kafka close must not extend a delegation's terminal emit budget."""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_event_emit_effect.handlers import (
    handler_event_emit_effect as module,
)
from omnimarket.nodes.node_event_emit_effect.models.model_emit_request import (
    ModelEmitRequest,
)
from omnimarket.nodes.node_event_emit_effect.spool.spool_outbox import SpoolOutbox

pytestmark = pytest.mark.unit


class SlowCloseBroker:
    def __init__(self, *, fail_publish: bool = False) -> None:
        self.fail_publish = fail_publish
        self.close_started = threading.Event()
        self.close_cancelled = threading.Event()
        self.messages = 0

    async def start(self) -> None:
        pass

    async def publish(self, **kwargs: Any) -> None:
        await asyncio.sleep(0.04)
        if self.fail_publish:
            raise RuntimeError("broker failed after connecting")
        self.messages += 1

    async def close(self) -> None:
        self.close_started.set()
        try:
            await asyncio.sleep(30)
        finally:
            self.close_cancelled.set()


@pytest.mark.parametrize("fail_publish", [False, True])
def test_terminal_emit_close_uses_remaining_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_publish: bool
) -> None:
    broker = SlowCloseBroker(fail_publish=fail_publish)
    publisher = module.KafkaEventPublisher(
        "fake-bootstrap:9092", bus_factory=lambda: broker, publish_timeout_seconds=0.5
    )
    spool = SpoolOutbox(tmp_path / "spool")
    # The handler fixes its shared deadline before it opens the session, so the
    # deadline handed to session() is the moment the 0.1 s drain budget ends.
    # Topic resolution, enrichment and the spool's fsyncs run before that point
    # and vary with the host, so the bound times only what follows it.
    session_deadlines: list[float] = []
    real_session = publisher.session

    def recording_session(*, deadline: float | None = None) -> Any:
        assert deadline is not None
        session_deadlines.append(deadline)
        return real_session(deadline=deadline)

    monkeypatch.setattr(publisher, "session", recording_session)
    handler = module.HandlerEventEmitEffect(
        spool=spool, publish_adapter=publisher, drain_budget_seconds=0.1
    )
    result = handler.handle(
        ModelEmitRequest(event_type="session.started", payload={"session_id": "budget"})
    )
    finished = time.monotonic()
    assert len(session_deadlines) == 1
    cleanup_after_budget = finished - session_deadlines[0]
    assert result.published is not fail_publish
    assert spool.pending_count() == int(fail_publish)
    # A cleanup that restarted the budget would wait out the 0.5 s publish
    # timeout (the broker's close sleeps 30 s) and land past this bound.
    assert cleanup_after_budget < 0.3, (
        f"cleanup restarted the budget: {cleanup_after_budget:.3f}s after the deadline"
    )
    assert broker.close_started.wait(1)
    assert broker.close_cancelled.wait(1), (
        "timed-out close kept running on the shared loop"
    )


def test_background_loop_timeout_cancels_its_own_work() -> None:
    started = threading.Event()
    cancelled = threading.Event()

    async def work() -> None:
        started.set()
        try:
            await asyncio.sleep(30)
        finally:
            cancelled.set()

    with pytest.raises(TimeoutError):
        module._KafkaPublishLoop.shared().run(work(), timeout=0.05)
    assert started.wait(1)
    assert cancelled.wait(1), "caller timed out but its coroutine was left running"
