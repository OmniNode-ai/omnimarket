# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-13467 semantics after the OMN-15469 single-producer correction.

OMN-13467 originally made ``HandlerGenerationConsumer`` await a self-published
terminal before returning. Canonical definition-B wiring now publishes every
typed handler return through its result applier, so that old self-publish became
a second producer. The durable order is owned by wiring: handler returns a
``ModelGenerationBenchmark`` -> result applier awaits terminal publication ->
the consume callback returns. This module keeps the useful restart and async
publisher coverage while pinning the corrected ownership boundary:

  1. Handler-owned ancillary events still await async publisher completion.
  2. Sync ancillary publishers remain supported.
  3. Fresh and replay paths return a benchmark and emit zero terminal topics.
  4. Replay skips repeated deploy/registration/tool-reuse side effects while
     returning the stored benchmark for wiring to publish.
  5. A publisher that would fail on a terminal topic is unreachable from the
     handler; terminal failure handling belongs to runtime wiring.
"""

from __future__ import annotations

import asyncio
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
# 1. Async ancillary publisher is awaited before handle() returns
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_async_publisher_is_awaited_for_ancillary_events() -> None:
    """Handler-owned publishes complete before handle returns its benchmark.

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

    result = await handler.handle(
        ModelNodeGenerationRequest(
            task_description="Build a stub node",
            correlation_id="omn-13467-async-ack",
        )
    )

    assert result.contract_passed is True
    assert any("tool-reuse-match-requested" in topic for topic in acked)
    assert any("node-deploy" in topic for topic in acked)
    assert any("node-registration" in topic for topic in acked)
    assert not any(
        "generation-completed" in topic or "generation-failed" in topic
        for topic in acked
    ), "definition-B wiring, not the handler publisher, owns the terminal"


# ---------------------------------------------------------------------------
# 2. Sync publisher backwards-compat (existing tests should still pass)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sync_publisher_still_works() -> None:
    """Sync ancillary publishers are called transparently by _await_publish."""
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
    assert any("node-deploy" in topic for topic in topics)
    assert any("node-registration" in topic for topic in topics)
    assert not any(
        "generation-completed" in topic or "generation-failed" in topic
        for topic in topics
    )


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
# 4. Restart path: return replay benchmark without repeating side effects
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_restart_path_returns_benchmark_without_repeating_side_effects(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Redelivery returns stored output for wiring without repeating side effects.

    Scenario:
      Run 1: handle() runs normally, publishes ancillary deploy/registration,
             writes replay state, and returns the terminal benchmark to wiring.
      Run 2: input re-delivered (same correlation_id). The handler must:
             a. Detect the replay marker from Run 1.
             b. Return the stored benchmark for wiring to publish.
             c. Emit no handler-owned ancillary events again.

    Terminal acknowledgement and input-offset ordering are outside this handler
    at the definition-B result-applier boundary. This test deliberately records
    only the publisher injected into the handler, where terminal traffic must be
    absent on both runs.
    """
    state_dir = tmp_path / "onex_state"
    monkeypatch.setenv("ONEX_STATE_DIR", str(state_dir))

    published: list[tuple[str, bytes]] = []
    ack_order: list[str] = []

    async def _publisher(topic: str, payload: bytes) -> None:
        """Async handler-owned publisher: records completed side effects."""
        await asyncio.sleep(0)
        ack_order.append(topic)
        published.append((topic, payload))

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

    assert not any(
        "generation-completed" in topic or "generation-failed" in topic
        for topic in ack_order
    )
    assert any("node-deploy" in topic for topic in ack_order)
    assert any("node-registration" in topic for topic in ack_order)
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

    assert ack_order == ack_order_before_run2, (
        "replay must not repeat tool-reuse/deploy/registration publications"
    )
    assert len(published) == len(ack_order_before_run2)
    assert result2 == result1


# ---------------------------------------------------------------------------
# 5. Terminal-named publisher failures are unreachable from the handler
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_terminal_named_publisher_failure_is_unreachable() -> None:
    """A terminal-only failure cannot fire through the ancillary publisher."""
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

    result = await handler.handle(
        ModelNodeGenerationRequest(
            task_description="Build a stub node",
            correlation_id="omn-13467-publish-fail",
        )
    )

    assert result.contract_passed is True
    assert not any(
        "generation-completed" in t or "generation-failed" in t for t in published
    ), "terminal publication belongs exclusively to definition-B wiring"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handler_writes_replay_state_before_wiring_terminal_boundary() -> None:
    """Stored output lets a redelivery hand the same benchmark back to wiring."""
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

    first = await handler.handle(
        ModelNodeGenerationRequest(
            task_description="Build a stub node",
            correlation_id=correlation_id,
        )
    )

    assert replay_path.exists()

    replayed = await handler.handle(
        ModelNodeGenerationRequest(
            task_description="Build a stub node",
            correlation_id=correlation_id,
        )
    )
    assert replayed == first
