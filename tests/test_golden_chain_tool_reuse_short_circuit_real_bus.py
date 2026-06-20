# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tool-reuse short-circuit real bus-backed golden chain (OMN-13356).

Proves the wiring that finishes OMN-13356: before generating, the generation
consumer publishes a ``tool-reuse-match-requested`` command on the bus; the REAL
``node_tool_reuse_matcher_compute`` handler (subscribed to that command) replies
with a matched / no-match verdict event; the generation consumer consumes the
verdict over the bus and either short-circuits (returns the existing tool, NO LLM
call) or proceeds to fresh generation.

This is a REAL bus-level chain, not a handler-isolation in-process call
(memory ``feedback_real_dispatch_path_tests``):

  * the transport is ``EventBusInmemory`` (the same in-memory bus the runtime
    uses for offline proof), not a hand-rolled fake;
  * the generation consumer publishes the command and awaits the verdict through
    injected sync bus adapters that bridge to the async bus via
    ``run_coroutine_threadsafe`` — the same sync-over-async shape the production
    Kafka adapter presents;
  * the matcher is the real ``HandlerToolReuseMatcher`` over a real
    ``InMemoryGeneratedToolRegistry``, subscribed to the contract-declared
    request topic and publishing on the contract-declared verdict topics;
  * topics are read from each node's ``contract.yaml`` — never hardcoded here.

Chain verified:
  generation handle()
    -> publish tool-reuse-match-requested.v1            (bus)
    -> matcher consumes + publishes matched / no-match   (bus)
    -> generation consumes the verdict                   (bus)
    -> MATCHED  : short-circuit (reused_tool_id set, zero attempts, LLM unused)
       NO_MATCH : proceed to generation (LLM called, benchmark from fresh artifact)
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_generation_consumer.handlers.handler_generation_consumer import (
    HandlerGenerationConsumer,
)
from omnimarket.nodes.node_generation_consumer.models.model_generation import (
    ModelGenerationBenchmark,
    ModelNodeGenerationRequest,
)
from omnimarket.nodes.node_tool_reuse_matcher_compute.handlers.handler_tool_reuse_matcher import (
    HandlerToolReuseMatcher,
)
from omnimarket.nodes.node_tool_reuse_matcher_compute.models.model_generated_tool import (
    ModelGeneratedToolRecord,
)
from omnimarket.nodes.node_tool_reuse_matcher_compute.models.model_tool_reuse_enums import (
    EnumToolReuseVerdict,
)
from omnimarket.nodes.node_tool_reuse_matcher_compute.models.model_tool_reuse_request import (
    ModelToolReuseRequest,
)
from omnimarket.nodes.node_tool_reuse_matcher_compute.registry_in_memory import (
    InMemoryGeneratedToolRegistry,
)

_NODES_ROOT = Path("src/omnimarket/nodes")
_TASK_DESCRIPTION = "Scan source text and return findings with a count"


# ---------------------------------------------------------------------------
# Contract-declared topic resolution (never hardcoded)
# ---------------------------------------------------------------------------


def _topics(node_name: str) -> dict[str, list[str]]:
    raw = yaml.safe_load((_NODES_ROOT / node_name / "contract.yaml").read_text())
    event_bus: dict[str, list[str]] = raw["event_bus"]
    return event_bus


def _single(topics: list[str], fragment: str) -> str:
    matches = [t for t in topics if fragment in t]
    assert len(matches) == 1, f"expected one topic matching {fragment!r}, got {matches}"
    return matches[0]


# ---------------------------------------------------------------------------
# Deterministic LLM effect — proves whether generation actually ran.
# ---------------------------------------------------------------------------

_VALID_LLM_RESPONSE = (
    "```yaml\n"
    "name: node_stub_compute\n"
    'contract_version: "1.0.0"\n'
    "node_type: compute\n"
    "input_model:\n"
    "  name: ModelStubInput\n"
    "  module: omnimarket.nodes.node_stub_compute.models\n"
    "output_model:\n"
    "  name: ModelStubOutput\n"
    "  module: omnimarket.nodes.node_stub_compute.models\n"
    "```\n\n"
    "```python\n"
    "def handle(input_data):\n"
    '    return {"result": input_data}\n'
    "```\n"
)


class _Usage:
    def __init__(self) -> None:
        self.tokens_input = 10
        self.tokens_output = 20
        self.tokens_total = 30
        self.usage_source = "api"


class _Response:
    def __init__(self, text: str) -> None:
        self.generated_text = text
        self.usage = _Usage()
        self.latency_ms = 100.0


class _CountingEffect:
    """Injected LLM effect that records how many times it was called.

    A MATCHED short-circuit must NEVER call this; a NO_MATCH run must call it at
    least once. The call count is the load-bearing proof of the short-circuit.
    """

    def __init__(self) -> None:
        self.call_count = 0

    async def handle(self, request: Any) -> _Response:
        await asyncio.sleep(0)
        self.call_count += 1
        return _Response(_VALID_LLM_RESPONSE)


def _tool_record(tool_id: str, description: str) -> ModelGeneratedToolRecord:
    return ModelGeneratedToolRecord(
        tool_id=tool_id,
        tool_name=f"node_generated_{tool_id}",
        handler_module=f"omnimarket.generated.{tool_id}.handler",
        handler_class="HandlerGenerated",
        contract_hash=f"sha256:{tool_id}",
        semantic_description=description,
        input_model_name="ModelScanRequest",
        output_model_name="ModelScanResult",
        input_fields_hash="sha256:in",
        output_fields_hash="sha256:out",
        generated_at=datetime(2026, 6, 19, 12, 0, tzinfo=UTC),
        is_active=True,
    )


# ---------------------------------------------------------------------------
# Real matcher node wiring: subscribe to the request command, publish a verdict.
# ---------------------------------------------------------------------------


async def _wire_matcher(
    bus: EventBusInmemory,
    *,
    registry_tools: list[ModelGeneratedToolRecord],
) -> Callable[[], Awaitable[None]]:
    matcher_topics = _topics("node_tool_reuse_matcher_compute")
    request_topic = _single(matcher_topics["subscribe_topics"], "match-requested")
    matched_topic = _single(matcher_topics["publish_topics"], "tool-reuse-matched")
    no_match_topic = _single(matcher_topics["publish_topics"], "tool-reuse-no-match")

    matcher = HandlerToolReuseMatcher(InMemoryGeneratedToolRegistry(registry_tools))

    async def on_message(message: Any) -> None:
        payload = json.loads(message.value.decode("utf-8"))
        request = ModelToolReuseRequest.model_validate(payload)
        result = matcher.handle(request)
        terminal = (
            matched_topic
            if result.verdict == EnumToolReuseVerdict.MATCHED
            else no_match_topic
        )
        await bus.publish(
            terminal,
            key=str(result.correlation_id).encode(),
            value=json.dumps(result.model_dump(mode="json")).encode("utf-8"),
        )

    return await bus.subscribe(
        request_topic,
        on_message=on_message,
        group_id="test-tool-reuse-matcher-real-handler",
    )


# ---------------------------------------------------------------------------
# Bus-backed sync adapters bridging the generation consumer (sync) to the
# async bus, plus a verdict collector keyed by correlation_id.
# ---------------------------------------------------------------------------


class _VerdictCollector:
    """Subscribes to the verdict topics and records payloads by correlation_id."""

    def __init__(self) -> None:
        self._verdicts: dict[str, dict[str, Any]] = {}
        self._events: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    def _event_for(self, correlation_id: str) -> threading.Event:
        with self._lock:
            return self._events.setdefault(correlation_id, threading.Event())

    async def on_message(self, message: Any) -> None:
        payload = json.loads(message.value.decode("utf-8"))
        correlation_id = str(payload.get("correlation_id", ""))
        if not correlation_id:
            return
        with self._lock:
            self._verdicts[correlation_id] = payload
        self._event_for(correlation_id).set()

    def wait(
        self, correlation_id: str, timeout_seconds: float
    ) -> dict[str, Any] | None:
        if not self._event_for(correlation_id).wait(timeout=timeout_seconds):
            return None
        with self._lock:
            return self._verdicts.get(correlation_id)


def _make_publisher(
    bus: EventBusInmemory, loop: asyncio.AbstractEventLoop
) -> Callable[[str, bytes], None]:
    def publish(topic: str, payload: bytes) -> None:
        future = asyncio.run_coroutine_threadsafe(
            bus.publish(topic, key=None, value=payload), loop
        )
        # Block until the publish (and its inline subscriber callbacks) complete,
        # so the matcher has produced its verdict before the consumer waits.
        future.result(timeout=10.0)

    return publish


def _make_consumer(
    collector: _VerdictCollector,
    matched_topic: str,
) -> Callable[[str, str, float], dict[str, Any] | None]:
    def consume(
        topic: str, correlation_id: str, timeout_seconds: float
    ) -> dict[str, Any] | None:
        # The generation consumer waits on the MATCHED topic; the collector has
        # both verdict terminals, so we only surface a payload whose verdict is
        # MATCHED on this topic. A NO_MATCH verdict (or no verdict) returns None,
        # which the consumer treats as "proceed to generation".
        assert topic == matched_topic
        payload = collector.wait(correlation_id, timeout_seconds)
        if payload is None:
            return None
        if str(payload.get("verdict", "")).lower() != "matched":
            return None
        return payload

    return consume


async def _run_generation_over_bus(
    bus: EventBusInmemory,
    *,
    registry_tools: list[ModelGeneratedToolRecord],
    task_description: str,
    correlation_id: str,
) -> tuple[ModelGenerationBenchmark, _CountingEffect]:
    """Drive a full generation request through the real bus and return the result."""
    loop = asyncio.get_running_loop()
    collector = _VerdictCollector()

    gen_topics = _topics("node_generation_consumer")
    matched_topic = _single(gen_topics["subscribe_topics"], "tool-reuse-matched")
    no_match_topic = _single(gen_topics["subscribe_topics"], "tool-reuse-no-match")

    unsub_matcher = await _wire_matcher(bus, registry_tools=registry_tools)
    unsub_matched = await bus.subscribe(
        matched_topic,
        on_message=collector.on_message,
        group_id="test-generation-verdict-collector-matched",
    )
    unsub_no_match = await bus.subscribe(
        no_match_topic,
        on_message=collector.on_message,
        group_id="test-generation-verdict-collector-no-match",
    )

    effect = _CountingEffect()
    handler = HandlerGenerationConsumer(
        effect_handler=effect,
        event_publisher=_make_publisher(bus, loop),
        event_consumer=_make_consumer(collector, matched_topic),
    )
    command = ModelNodeGenerationRequest(
        task_description=task_description,
        correlation_id=correlation_id,
        max_attempts=1,
    )
    try:
        # Run the sync-blocking handle() off the event loop so the loop is free to
        # deliver the matcher's verdict while the consumer adapter blocks.
        benchmark = await asyncio.to_thread(asyncio.run, handler.handle(command))
    finally:
        for unsubscribe in (unsub_matched, unsub_no_match, unsub_matcher):
            await unsubscribe()
    return benchmark, effect


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_match_short_circuits_generation_over_real_bus(
    event_bus: EventBusInmemory,
) -> None:
    """A reusable tool exists -> matcher MATCHES -> generation is short-circuited.

    Proves: the command is published, the matcher emits a matched verdict on the
    bus, the generation consumer consumes it and returns a reuse benchmark with
    zero attempts, zero cost, the reused tool id set, and the LLM effect NEVER
    called.
    """
    await event_bus.start()
    correlation_id = "OMN-13356-real-bus-match"
    # A registry tool whose description is identical to the task -> semantic
    # similarity 1.0 clears the matcher's default 0.85 threshold -> MATCHED.
    tool = _tool_record(tool_id="existing-scanner", description=_TASK_DESCRIPTION)
    try:
        benchmark, effect = await _run_generation_over_bus(
            event_bus,
            registry_tools=[tool],
            task_description=_TASK_DESCRIPTION,
            correlation_id=correlation_id,
        )

        # --- short-circuit proof ---
        assert benchmark.reused_tool_id == "existing-scanner"
        assert benchmark.attempt_count == 0
        assert benchmark.attempts == []
        assert benchmark.cost_inference_usd == 0.0
        assert benchmark.contract_passed is True
        assert benchmark.cost_basis == "tool_reuse"
        # The LLM was never called — the whole point of reuse.
        assert effect.call_count == 0

        # --- bus-level proof: command published + matched verdict emitted ---
        gen_topics = _topics("node_generation_consumer")
        request_topic = _single(
            gen_topics["publish_topics"], "tool-reuse-match-requested"
        )
        matched_topic = _single(gen_topics["subscribe_topics"], "tool-reuse-matched")
        no_match_topic = _single(gen_topics["subscribe_topics"], "tool-reuse-no-match")
        assert len(await event_bus.get_event_history(topic=request_topic)) == 1
        assert len(await event_bus.get_event_history(topic=matched_topic)) == 1
        assert len(await event_bus.get_event_history(topic=no_match_topic)) == 0
    finally:
        await event_bus.close()


@pytest.mark.asyncio
async def test_no_match_proceeds_to_generation_over_real_bus(
    event_bus: EventBusInmemory,
) -> None:
    """No reusable tool -> matcher NO_MATCH -> generation runs (LLM called).

    Proves the negative arm of the chain over the real bus: the matcher emits a
    no-match verdict, the generation consumer does NOT short-circuit, the LLM
    effect IS called, and the resulting benchmark is a fresh generation (no
    reused_tool_id).
    """
    await event_bus.start()
    correlation_id = "OMN-13356-real-bus-no-match"
    # A registry tool whose description shares no tokens with the task -> semantic
    # similarity below threshold -> NO_MATCH.
    tool = _tool_record(
        tool_id="unrelated-tool",
        description="Render a markdown table from CSV rows",
    )
    try:
        benchmark, effect = await _run_generation_over_bus(
            event_bus,
            registry_tools=[tool],
            task_description=_TASK_DESCRIPTION,
            correlation_id=correlation_id,
        )

        # --- proceeded-to-generation proof ---
        assert benchmark.reused_tool_id == ""
        assert benchmark.attempt_count == 1
        assert benchmark.contract_passed is True
        # The LLM WAS called exactly once — generation actually ran.
        assert effect.call_count == 1

        # --- bus-level proof: command published + no-match verdict emitted ---
        gen_topics = _topics("node_generation_consumer")
        request_topic = _single(
            gen_topics["publish_topics"], "tool-reuse-match-requested"
        )
        matched_topic = _single(gen_topics["subscribe_topics"], "tool-reuse-matched")
        no_match_topic = _single(gen_topics["subscribe_topics"], "tool-reuse-no-match")
        assert len(await event_bus.get_event_history(topic=request_topic)) == 1
        assert len(await event_bus.get_event_history(topic=matched_topic)) == 0
        assert len(await event_bus.get_event_history(topic=no_match_topic)) == 1
    finally:
        await event_bus.close()


@pytest.mark.asyncio
async def test_empty_registry_proceeds_to_generation_over_real_bus(
    event_bus: EventBusInmemory,
) -> None:
    """An empty tool registry -> NO_MATCH -> generation runs (first-ever tool)."""
    await event_bus.start()
    try:
        benchmark, effect = await _run_generation_over_bus(
            event_bus,
            registry_tools=[],
            task_description=_TASK_DESCRIPTION,
            correlation_id="OMN-13356-real-bus-empty-registry",
        )
        assert benchmark.reused_tool_id == ""
        assert effect.call_count == 1
        assert benchmark.attempt_count == 1
    finally:
        await event_bus.close()
