# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-13467: offset commit is AFTER terminal broker-ack (restart-path proof).

DoD assertions:
  1. _emit_benchmark is awaited — handle() does not return until the terminal
     event publish has completed (or its awaitable has been scheduled).
  2. Crash-between-publish-and-commit restart path: when handle() completes a
     first run (terminal published, replay state written) and the input is
     re-delivered (simulating a missed offset commit), the handler returns the
     stored benchmark WITHOUT re-emitting. A fresh subscriber that consumed the
     terminal on the first run still sees it; no event loss occurs.
  3. Async publisher is awaited: when an async publisher is injected, _await_publish
     suspends until the coroutine completes before handle() returns.
  4. Sync publisher backwards-compat: a sync publisher (legacy test pattern) still
     works transparently — _await_publish calls it and ignores the None return.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from omnimarket.nodes.node_generation_consumer.handlers.handler_generation_consumer import (
    HandlerGenerationConsumer,
    _await_publish,
    _replay_state_path,
)
from omnimarket.nodes.node_generation_consumer.models.model_generation import (
    ModelNodeGenerationRequest,
)

# ---------------------------------------------------------------------------
# Shared test fixtures
# ---------------------------------------------------------------------------

_VALID_CONTRACT_YAML = """\
name: node_stub_compute
contract_version: "1.0.0"
node_type: compute
input_model:
  name: ModelStubInput
  module: omnimarket.nodes.node_stub_compute.models
output_model:
  name: ModelStubOutput
  module: omnimarket.nodes.node_stub_compute.models
"""

_VALID_HANDLER_SOURCE = """\
def handle(input_data):
    return {"result": input_data}
"""

_VALID_LLM_RESPONSE = (
    "Here is your node:\n"
    "```yaml\n" + _VALID_CONTRACT_YAML + "```\n\n"
    "```python\n" + _VALID_HANDLER_SOURCE + "```\n"
)


class _FakeUsage:
    def __init__(self) -> None:
        self.tokens_input = 10
        self.tokens_output = 20
        self.tokens_total = 30
        self.usage_source = "api"


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self.generated_text = text
        self.usage = _FakeUsage()
        self.latency_ms = 50.0


class _FakeLlmEffect:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)

    async def handle(self, request: Any) -> _FakeResponse:
        await asyncio.sleep(0)
        text = self._responses.pop(0) if self._responses else _VALID_LLM_RESPONSE
        return _FakeResponse(text)


@pytest.fixture(autouse=True)
def _isolate_onex_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """Point replay-state at a temp dir so tests are hermetic."""
    monkeypatch.setenv("ONEX_STATE_DIR", str(tmp_path / "onex_state"))
    monkeypatch.delenv("ONEX_STATE_ROOT", raising=False)


# ---------------------------------------------------------------------------
# 1. Async publisher is awaited before handle() returns (OMN-13467 DoD)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_async_publisher_is_awaited_before_handle_returns() -> None:
    """OMN-13467: handle() must await the terminal publish before returning.

    We inject an async publisher that sets a flag ONLY after its coroutine
    body runs. If handle() returned before awaiting the coroutine, the flag
    would still be False on the caller's side.
    """
    acked: list[str] = []

    async def _async_publisher(topic: str, payload: bytes) -> None:
        # Yield to the event loop to prove we actually suspend + resume.
        await asyncio.sleep(0)
        acked.append(topic)

    handler = HandlerGenerationConsumer(
        effect_handler=_FakeLlmEffect([_VALID_LLM_RESPONSE]),
        event_publisher=_async_publisher,
    )

    await handler.handle(
        ModelNodeGenerationRequest(
            task_description="Build a stub node",
            correlation_id="omn-13467-async-ack",
        )
    )

    # At least the terminal (generation-completed / generation-failed) must be
    # among the acked topics; the async publish completed before handle() returned.
    terminal_acked = any(
        "generation-completed" in t or "generation-failed" in t for t in acked
    )
    assert terminal_acked, (
        f"Terminal event was not broker-ACKed before handle() returned. "
        f"Acked topics: {acked}"
    )


# ---------------------------------------------------------------------------
# 2. Sync publisher backwards-compat (existing tests should still pass)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sync_publisher_still_works() -> None:
    """Sync publishers (test pattern) are called transparently by _await_publish."""
    published: list[tuple[str, bytes]] = []

    def _sync_publisher(topic: str, payload: bytes) -> None:
        published.append((topic, payload))

    handler = HandlerGenerationConsumer(
        effect_handler=_FakeLlmEffect([_VALID_LLM_RESPONSE]),
        event_publisher=_sync_publisher,
    )

    result = await handler.handle(
        ModelNodeGenerationRequest(
            task_description="Build a stub node",
            correlation_id="omn-13467-sync-compat",
        )
    )

    assert result.contract_passed is True
    topics = [t for t, _ in published]
    assert any("generation-completed" in t for t in topics)


# ---------------------------------------------------------------------------
# 3. _await_publish helper: awaits coroutines, ignores None from sync callers
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_await_publish_awaits_async_publisher() -> None:
    """_await_publish suspends until the async publisher's coroutine completes."""
    completed: list[bool] = []

    async def _async_pub(topic: str, payload: bytes) -> None:
        await asyncio.sleep(0)
        completed.append(True)

    await _await_publish(_async_pub, "test-topic", b"payload")
    assert completed == [True]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_await_publish_is_noop_for_sync_publisher() -> None:
    """_await_publish does not raise when the publisher returns None (sync)."""
    calls: list[str] = []

    def _sync_pub(topic: str, payload: bytes) -> None:
        calls.append(topic)

    await _await_publish(_sync_pub, "test-topic", b"payload")
    assert calls == ["test-topic"]


# ---------------------------------------------------------------------------
# 4. Restart-path: no terminal-event loss when offset commit was missed
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_restart_path_no_terminal_loss_on_redelivery(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OMN-13467 DoD: crash-between-publish-and-commit causes no event loss.

    Scenario:
      Run 1: handle() runs normally — terminal event published and acked,
             replay marker written. Simulate a crash BEFORE offset commit by
             NOT committing (we simply don't call commit in this test).
      Run 2: input re-delivered (same correlation_id). The handler must:
             a. Detect the replay marker from Run 1.
             b. Return stored benchmark WITHOUT re-emitting (idempotent).
             c. A fresh subscriber still sees the terminal from Run 1.

    The terminal event published in Run 1 is durably on the bus; a fresh
    subscriber sees it regardless of whether Run 2 re-emits. Because the
    handler does NOT re-emit on re-delivery (replay guard), the fresh
    subscriber sees exactly one copy — no duplication, no loss.
    """
    state_dir = tmp_path / "onex_state"
    monkeypatch.setenv("ONEX_STATE_DIR", str(state_dir))

    # Shared bus — represents what a fresh subscriber would see.
    bus: list[tuple[str, bytes]] = []
    ack_order: list[str] = []

    async def _publisher(topic: str, payload: bytes) -> None:
        """Async publisher: records ack order + appends to bus."""
        await asyncio.sleep(0)
        ack_order.append(topic)
        bus.append((topic, payload))

    # --- Run 1: normal execution ---
    handler_run1 = HandlerGenerationConsumer(
        effect_handler=_FakeLlmEffect([_VALID_LLM_RESPONSE]),
        event_publisher=_publisher,
    )
    correlation_id = "omn-13467-restart-proof"

    result1 = await handler_run1.handle(
        ModelNodeGenerationRequest(
            task_description="Build a stub node",
            correlation_id=correlation_id,
        )
    )
    assert result1.contract_passed is True

    # Confirm terminal was published and acked before handle() returned.
    terminal_topics_run1 = [
        t for t in ack_order if "generation-completed" in t or "generation-failed" in t
    ]
    assert terminal_topics_run1, (
        "Run 1: terminal event was not acked before handle() returned"
    )
    # Verify replay marker was written.
    replay_files = list(state_dir.rglob("*.json"))
    assert replay_files, "Replay marker must be written after successful run"

    # --- Simulate crash (offset NOT committed). Re-deliver the same input. ---
    ack_order_before_run2 = list(ack_order)

    # --- Run 2: re-delivery of the same correlation_id ---
    handler_run2 = HandlerGenerationConsumer(
        effect_handler=_FakeLlmEffect([_VALID_LLM_RESPONSE]),  # not called on replay
        event_publisher=_publisher,
    )

    result2 = await handler_run2.handle(
        ModelNodeGenerationRequest(
            task_description="Build a stub node",
            correlation_id=correlation_id,
        )
    )

    # Run 2 must return the stored benchmark (no re-LLM-call).
    assert result2.correlation_id == result1.correlation_id
    assert result2.contract_passed == result1.contract_passed

    # Run 2 must NOT have re-emitted the terminal event — replay guard.
    new_terminal_emits = [
        t
        for t in ack_order[len(ack_order_before_run2) :]
        if "generation-completed" in t or "generation-failed" in t
    ]
    assert new_terminal_emits == [], (
        "Run 2 must NOT re-emit the terminal event (replay guard). "
        f"New terminal emits: {new_terminal_emits}"
    )

    # The bus still has the terminal from Run 1 — a fresh subscriber sees it.
    terminal_on_bus = [
        (t, p)
        for t, p in bus
        if "generation-completed" in t or "generation-failed" in t
    ]
    assert len(terminal_on_bus) >= 1, (
        "A fresh subscriber must see the terminal event from Run 1. "
        "Bus state: " + str([t for t, _ in bus])
    )

    # Exactly ONE terminal on the bus (no duplication from re-delivery).
    assert len(terminal_on_bus) == 1, (
        f"Exactly one terminal event expected; got {len(terminal_on_bus)}. "
        f"Duplicate terminals would violate at-most-once for consumers."
    )

    # Verify the terminal payload is parseable and correct.
    _terminal_topic, terminal_payload = terminal_on_bus[0]
    data = json.loads(terminal_payload)
    assert data["correlation_id"] == correlation_id
    assert data["contract_passed"] is True


# ---------------------------------------------------------------------------
# 5. Exception in async publisher propagates (no silent loss)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_terminal_publish_failure_propagates_and_blocks_offset_commit() -> None:
    """OMN-13467: a failing TERMINAL publish must propagate out of handle().

    The wiring layer (at-least-once consumer) commits the input Kafka offset only
    after handle() returns WITHOUT raising. If the broker does not ACK the
    terminal event (node-generation-completed/failed), handle() MUST raise so the
    offset is NOT committed — the input is then re-delivered and the terminal is
    re-emitted. Swallowing the publish error here would advance the offset past an
    unpublished terminal: the exact at-most-once gap this ticket closes.

    This test pins the failure mode: if someone re-wraps the terminal emit in a
    try/except that logs-and-continues, handle() would stop raising and this test
    goes red — catching the regression.
    """
    published: list[str] = []

    async def _failing_publisher(topic: str, payload: bytes) -> None:
        await asyncio.sleep(0)
        published.append(topic)
        if "generation-completed" in topic or "generation-failed" in topic:
            raise RuntimeError("broker ack timeout")
        # Non-terminal topics (deploy, registration) succeed.

    handler = HandlerGenerationConsumer(
        effect_handler=_FakeLlmEffect([_VALID_LLM_RESPONSE]),
        event_publisher=_failing_publisher,
    )

    # The terminal publish raises → handle() must propagate it (no swallow) so the
    # wiring layer never commits the offset.
    with pytest.raises(RuntimeError, match="broker ack timeout"):
        await handler.handle(
            ModelNodeGenerationRequest(
                task_description="Build a stub node",
                correlation_id="omn-13467-publish-fail",
            )
        )

    # The handler did attempt to publish the terminal event before raising.
    assert any(
        "generation-completed" in t or "generation-failed" in t for t in published
    ), "Handler must attempt the terminal publish before propagating the broker error"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_terminal_publish_failure_does_not_write_replay_marker() -> None:
    """OMN-13467: when the terminal publish fails, the replay marker must NOT be
    written. handle() raises before _record_replay_benchmark, so a re-delivery of
    the input re-runs generation and re-emits the terminal — no terminal loss.

    Uses the autouse _isolate_onex_state fixture's ONEX_STATE_DIR so the replay
    path is computed from the same root the handler writes to.
    """
    correlation_id = "omn-13467-no-replay-on-fail"
    replay_path = _replay_state_path(correlation_id)
    assert replay_path is not None, "state root must be isolated by the autouse fixture"

    async def _failing_publisher(topic: str, payload: bytes) -> None:
        await asyncio.sleep(0)
        if "generation-completed" in topic or "generation-failed" in topic:
            raise RuntimeError("broker ack timeout")

    handler = HandlerGenerationConsumer(
        effect_handler=_FakeLlmEffect([_VALID_LLM_RESPONSE]),
        event_publisher=_failing_publisher,
    )

    with pytest.raises(RuntimeError, match="broker ack timeout"):
        await handler.handle(
            ModelNodeGenerationRequest(
                task_description="Build a stub node",
                correlation_id=correlation_id,
            )
        )

    # No replay marker written → re-delivery re-runs and re-emits the terminal.
    assert not replay_path.exists(), (
        "Replay marker must NOT be written when the terminal publish failed "
        f"({replay_path}); otherwise re-delivery would skip re-emission"
    )
