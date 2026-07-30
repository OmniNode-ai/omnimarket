# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Single terminal-publication authority for generation def-B dispatch (OMN-15469).

``HandlerGenerationConsumer.handle`` is a canonical definition-B handler: its
typed ``ModelGenerationBenchmark`` return is the terminal handoff.  Runtime
wiring normalizes that return into an output event and publishes it through the
contract-selected terminal topic.  The handler may still publish ancillary
commands/events (tool-reuse request, deploy, registration, escalation), but it
must never publish the generation-completed/failed terminal itself.
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_generation_consumer.handlers import (
    handler_generation_consumer as generation_module,
)
from omnimarket.nodes.node_generation_consumer.handlers.handler_generation_consumer import (
    HandlerGenerationConsumer,
)
from omnimarket.nodes.node_generation_consumer.models.model_generation import (
    ModelGenerationBenchmark,
    ModelNodeGenerationRequest,
)

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
    "```yaml\n"
    + _VALID_CONTRACT_YAML
    + "```\n\n```python\n"
    + _VALID_HANDLER_SOURCE
    + "```\n"
)
_INVALID_LLM_RESPONSE = "I could not generate a valid node."


class _Usage:
    tokens_input = 10
    tokens_output = 20
    tokens_total = 30
    usage_source = "api"


class _Response:
    def __init__(self, text: str) -> None:
        self.generated_text = text
        self.usage = _Usage()
        self.latency_ms = 1.0


class _CountingEffect:
    def __init__(self, response: str) -> None:
        self.response = response
        self.call_count = 0

    async def handle(self, _request: Any) -> _Response:
        await asyncio.sleep(0)
        self.call_count += 1
        return _Response(self.response)


class _ForbiddenEffect:
    async def handle(self, _request: Any) -> _Response:
        raise AssertionError("tool reuse must short-circuit LLM inference")


def _terminal_topics(topics: list[str]) -> list[str]:
    return [
        topic
        for topic in topics
        if "generation-completed" in topic or "generation-failed" in topic
    ]


@pytest.fixture(autouse=True)
def _isolate_replay_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ONEX_STATE_DIR", str(tmp_path / "onex-state"))
    monkeypatch.delenv("ONEX_STATE_ROOT", raising=False)


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("llm_response", "contract_passed"),
    [
        pytest.param(_VALID_LLM_RESPONSE, True, id="completed"),
        pytest.param(_INVALID_LLM_RESPONSE, False, id="failed"),
    ],
)
async def test_handler_returns_benchmark_without_publishing_terminal(
    llm_response: str,
    contract_passed: bool,
) -> None:
    """Both verdicts return to wiring and emit zero raw terminal topics."""
    published_topics: list[str] = []
    handler = HandlerGenerationConsumer(
        effect_handler=_CountingEffect(llm_response),
        event_publisher=lambda topic, _payload: published_topics.append(topic),
    )

    benchmark = await handler.handle(
        ModelNodeGenerationRequest(
            task_description="Build a stub node",
            correlation_id=f"omn-15469-{contract_passed}",
            max_attempts=1,
        )
    )

    assert isinstance(benchmark, ModelGenerationBenchmark)
    assert benchmark.contract_passed is contract_passed
    assert _terminal_topics(published_topics) == [], (
        "definition-B wiring owns publication of the returned benchmark"
    )
    if contract_passed:
        assert any("node-deploy" in topic for topic in published_topics)
        assert any("node-registration" in topic for topic in published_topics)
    else:
        assert not any("node-deploy" in topic for topic in published_topics)
        assert not any("node-registration" in topic for topic in published_topics)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_replay_returns_same_benchmark_without_handler_publication() -> None:
    """A redelivery returns durable state for wiring to publish again."""
    published_topics: list[str] = []
    effect = _CountingEffect(_VALID_LLM_RESPONSE)
    handler = HandlerGenerationConsumer(
        effect_handler=effect,
        event_publisher=lambda topic, _payload: published_topics.append(topic),
    )
    command = ModelNodeGenerationRequest(
        task_description="Build a replay-safe stub node",
        correlation_id="omn-15469-replay",
        max_attempts=1,
    )

    first = await handler.handle(command)
    assert _terminal_topics(published_topics) == []
    published_topics.clear()

    replayed = await handler.handle(command)

    assert replayed == first
    assert effect.call_count == 1
    assert published_topics == [], (
        "replay must not repeat deploy/registration/tool-reuse side effects; "
        "the wiring layer publishes the returned benchmark"
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tool_reuse_returns_benchmark_and_only_publishes_match_request() -> None:
    """Tool reuse preserves its command publish but delegates its terminal."""
    published_topics: list[str] = []

    def _matched_verdict(
        _topic: str, _correlation_id: str, _timeout_seconds: float
    ) -> dict[str, object]:
        return {
            "verdict": "matched",
            "matched_tool": {"tool": {"tool_id": "existing-stub-tool"}},
        }

    handler = HandlerGenerationConsumer(
        effect_handler=_ForbiddenEffect(),
        event_publisher=lambda topic, _payload: published_topics.append(topic),
        event_consumer=_matched_verdict,
    )

    benchmark = await handler.handle(
        ModelNodeGenerationRequest(
            task_description="Build a reusable stub node",
            correlation_id="omn-15469-tool-reuse",
            max_attempts=1,
        )
    )

    assert benchmark.reused_tool_id == "existing-stub-tool"
    assert benchmark.attempt_count == 0
    assert _terminal_topics(published_topics) == []
    assert len(published_topics) == 1
    assert "tool-reuse-match-requested" in published_topics[0]


@pytest.mark.unit
def test_terminal_publication_is_structurally_absent_from_handler() -> None:
    """Prevent a dead self-publish helper from becoming a second producer again."""
    source = inspect.getsource(HandlerGenerationConsumer)

    assert "def _emit_benchmark" not in source
    assert "self._topic_completed" not in source
    assert "self._topic_failed" not in source


@pytest.mark.unit
def test_contract_keeps_terminal_topics_for_definition_b_wiring() -> None:
    """Removing handler publication must not remove wiring's route authority."""
    contract_path = (
        Path(generation_module.__file__).resolve().parent.parent / "contract.yaml"
    )
    contract = yaml.safe_load(contract_path.read_text())
    publish_topics = contract["event_bus"]["publish_topics"]

    assert contract["terminal_event"] in publish_topics
    assert contract["runtime_dispatch"]["terminal_events"] == {
        "success": "onex.evt.omnimarket.node-generation-completed.v1",
        "failure": "onex.evt.omnimarket.node-generation-failed.v1",
    }
