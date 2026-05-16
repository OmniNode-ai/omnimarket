# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for HandlerGenerationConsumer.

All tests use FakeLlmEffect — no network, no Kafka, no Docker.
The fake satisfies the HandlerLlmOpenaiCompatible interface:
    async def handle(request) -> response
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest

from omnimarket.nodes.node_generation_consumer.handlers.handler_generation_consumer import (
    HandlerGenerationConsumer,
    _extract_blocks,
    _validate_generation,
)
from omnimarket.nodes.node_generation_consumer.models.model_generation import (
    ModelNodeGenerationRequest,
)

# ---------------------------------------------------------------------------
# Fake LLM effect handler
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

_INVALID_CONTRACT_YAML = "not_a_mapping: [broken"

_INVALID_LLM_RESPONSE = (
    "```yaml\n" + _INVALID_CONTRACT_YAML + "\n```\n\n"
    "```python\n" + _VALID_HANDLER_SOURCE + "```\n"
)


class _FakeUsage:
    def __init__(self, inp: int = 10, out: int = 20) -> None:
        self.tokens_input = inp
        self.tokens_output = out
        self.tokens_total = inp + out


class _FakeResponse:
    def __init__(self, text: str, inp: int = 10, out: int = 20) -> None:
        self.generated_text = text
        self.usage = _FakeUsage(inp, out)
        self.latency_ms = 100.0


class FakeLlmEffect:
    """Deterministic fake — returns a fixed sequence of responses."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self._calls: list[Any] = []

    async def handle(self, request: Any) -> _FakeResponse:
        self._calls.append(request)
        text = self._responses.pop(0) if self._responses else _VALID_LLM_RESPONSE
        return _FakeResponse(text)


def _make_handler(
    responses: list[str],
    published: list[tuple[str, bytes]] | None = None,
) -> HandlerGenerationConsumer:
    captures: list[tuple[str, bytes]] = [] if published is None else published

    def _publisher(topic: str, payload: bytes) -> None:
        captures.append((topic, payload))

    handler = HandlerGenerationConsumer(
        effect_handler=FakeLlmEffect(responses),
        event_publisher=_publisher,
    )
    return handler


# ---------------------------------------------------------------------------
# Unit tests: _extract_blocks
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_extract_blocks_parses_yaml_and_python() -> None:
    contract_yaml, handler_source = _extract_blocks(_VALID_LLM_RESPONSE)
    assert "node_stub_compute" in contract_yaml
    assert "def handle" in handler_source


@pytest.mark.unit
def test_extract_blocks_falls_back_to_raw_when_no_yaml_fence() -> None:
    raw = "name: foo\ncontract_version: 1\n"
    contract_yaml, handler_source = _extract_blocks(raw)
    assert contract_yaml == raw
    assert handler_source == ""


# ---------------------------------------------------------------------------
# Unit tests: _validate_generation
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_validate_generation_passes_on_valid_input() -> None:
    result = _validate_generation(_VALID_CONTRACT_YAML, _VALID_HANDLER_SOURCE)
    assert result["valid"] is True
    assert result["errors"] == []
    assert "schema" in result["checks_passed"]
    assert "syntax" in result["checks_passed"]
    assert "security" in result["checks_passed"]


@pytest.mark.unit
def test_validate_generation_fails_on_missing_required_fields() -> None:
    minimal_yaml = "name: foo\nnode_type: compute\n"
    result = _validate_generation(minimal_yaml, _VALID_HANDLER_SOURCE)
    assert result["valid"] is False
    assert any("missing required fields" in e for e in result["errors"])


@pytest.mark.unit
def test_validate_generation_fails_on_syntax_error() -> None:
    bad_python = "def handle(:\n    pass\n"
    result = _validate_generation(_VALID_CONTRACT_YAML, bad_python)
    assert result["valid"] is False
    assert any("syntax error" in e for e in result["errors"])


@pytest.mark.unit
def test_validate_generation_fails_on_hardcoded_path() -> None:
    handler_with_path = 'def handle(x):\n    return "/Users/foo/bar"\n'
    result = _validate_generation(_VALID_CONTRACT_YAML, handler_with_path)
    assert result["valid"] is False
    assert any("hardcoded absolute path" in e for e in result["errors"])


@pytest.mark.unit
def test_validate_generation_fails_on_hardcoded_topic() -> None:
    handler_with_topic = 'def handle(x):\n    return "onex.cmd.omnimarket.foo.v1"\n'
    result = _validate_generation(_VALID_CONTRACT_YAML, handler_with_topic)
    assert result["valid"] is False
    assert any("hardcoded topic string" in e for e in result["errors"])


# ---------------------------------------------------------------------------
# Integration-style tests: handler.handle() with fake effect
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_passes_on_valid_generation() -> None:
    published: list[tuple[str, bytes]] = []
    handler = _make_handler([_VALID_LLM_RESPONSE], published=published)

    result = await handler.handle(
        ModelNodeGenerationRequest(
            task_description="Build a stub node",
            correlation_id="corr-valid-1",
        )
    )

    assert result.contract_passed is True
    assert result.attempt_count == 1
    assert result.correlation_id == "corr-valid-1"
    assert "node_stub_compute" in result.contract_yaml


@pytest.mark.unit
@pytest.mark.asyncio
async def test_retries_on_contract_failure_then_succeeds() -> None:
    published: list[tuple[str, bytes]] = []
    handler = _make_handler(
        [_INVALID_LLM_RESPONSE, _VALID_LLM_RESPONSE],
        published=published,
    )

    result = await handler.handle(
        ModelNodeGenerationRequest(
            task_description="Build a stub node",
            correlation_id="corr-retry-1",
            max_attempts=2,
        )
    )

    assert result.contract_passed is True
    assert result.attempt_count == 2
    # First attempt failed, second succeeded
    assert result.attempts[0].contract_passed is False
    assert result.attempts[1].contract_passed is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fails_after_max_attempts() -> None:
    published: list[tuple[str, bytes]] = []
    handler = _make_handler(
        [_INVALID_LLM_RESPONSE, _INVALID_LLM_RESPONSE],
        published=published,
    )

    result = await handler.handle(
        ModelNodeGenerationRequest(
            task_description="Build a stub node",
            correlation_id="corr-fail-1",
            max_attempts=2,
        )
    )

    assert result.contract_passed is False
    assert result.attempt_count == 2
    assert all(not a.contract_passed for a in result.attempts)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_emits_registration_on_success() -> None:
    published: list[tuple[str, bytes]] = []
    handler = _make_handler([_VALID_LLM_RESPONSE], published=published)

    await handler.handle(
        ModelNodeGenerationRequest(
            task_description="Build a stub node",
            correlation_id="corr-reg-1",
        )
    )

    topics = [t for t, _ in published]
    assert any("generation-completed" in t for t in topics)
    assert any("node-registered" in t for t in topics)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_no_registration_on_failure() -> None:
    published: list[tuple[str, bytes]] = []
    handler = _make_handler(
        [_INVALID_LLM_RESPONSE, _INVALID_LLM_RESPONSE],
        published=published,
    )

    await handler.handle(
        ModelNodeGenerationRequest(
            task_description="Build a stub node",
            correlation_id="corr-no-reg-1",
            max_attempts=2,
        )
    )

    topics = [t for t, _ in published]
    assert any("generation-failed" in t for t in topics)
    assert not any("node-registered" in t for t in topics)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_emits_completed_topic_on_success() -> None:
    published: list[tuple[str, bytes]] = []
    handler = _make_handler([_VALID_LLM_RESPONSE], published=published)

    await handler.handle(
        ModelNodeGenerationRequest(
            task_description="Build a stub node",
            correlation_id="corr-topic-1",
        )
    )

    assert any("generation-completed" in t for t, _ in published)
    assert not any("generation-failed" in t for t, _ in published)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_emits_failed_topic_on_failure() -> None:
    published: list[tuple[str, bytes]] = []
    handler = _make_handler([_INVALID_LLM_RESPONSE], published=published)

    await handler.handle(
        ModelNodeGenerationRequest(
            task_description="Build a stub node",
            correlation_id="corr-topic-2",
            max_attempts=1,
        )
    )

    assert any("generation-failed" in t for t, _ in published)
    assert not any("generation-completed" in t for t, _ in published)


# ---------------------------------------------------------------------------
# Deploy event tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_emits_deploy_event_on_success() -> None:
    published: list[tuple[str, bytes]] = []
    handler = _make_handler([_VALID_LLM_RESPONSE], published=published)

    await handler.handle(
        ModelNodeGenerationRequest(
            task_description="Build a stub node",
            correlation_id="corr-deploy-emit-1",
        )
    )

    topics = [t for t, _ in published]
    assert any("node-deploy" in t for t in topics)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_no_deploy_event_on_failure() -> None:
    published: list[tuple[str, bytes]] = []
    handler = _make_handler([_INVALID_LLM_RESPONSE], published=published)

    await handler.handle(
        ModelNodeGenerationRequest(
            task_description="Build a stub node",
            correlation_id="corr-deploy-fail-1",
            max_attempts=1,
        )
    )

    topics = [t for t, _ in published]
    assert not any("node-deploy" in t for t in topics)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_deploy_event_payload_has_hashes_and_source() -> None:

    published: list[tuple[str, bytes]] = []
    handler = _make_handler([_VALID_LLM_RESPONSE], published=published)

    await handler.handle(
        ModelNodeGenerationRequest(
            task_description="Build a stub node",
            correlation_id="corr-deploy-hash-1",
        )
    )

    deploy_events = [(t, p) for t, p in published if "node-deploy" in t]
    assert len(deploy_events) == 1

    payload = json.loads(deploy_events[0][1])
    assert payload["node_name"] == "node_stub_compute"
    assert "contract_yaml" in payload
    assert "handler_source" in payload
    assert payload["generated_contract_hash"].startswith("sha256:")
    assert payload["generated_handler_hash"].startswith("sha256:")

    # Verify hashes are correct
    expected_contract_hash = (
        "sha256:" + hashlib.sha256(payload["contract_yaml"].encode()).hexdigest()
    )
    expected_handler_hash = (
        "sha256:" + hashlib.sha256(payload["handler_source"].encode()).hexdigest()
    )
    assert payload["generated_contract_hash"] == expected_contract_hash
    assert payload["generated_handler_hash"] == expected_handler_hash


@pytest.mark.unit
@pytest.mark.asyncio
async def test_deploy_event_emitted_before_registration() -> None:
    """Deploy must arrive before registration so executor is ready when MCP tool fires."""
    published: list[tuple[str, bytes]] = []
    handler = _make_handler([_VALID_LLM_RESPONSE], published=published)

    await handler.handle(
        ModelNodeGenerationRequest(
            task_description="Build a stub node",
            correlation_id="corr-order-1",
        )
    )

    topics = [t for t, _ in published]
    deploy_idx = next(i for i, t in enumerate(topics) if "node-deploy" in t)
    registered_idx = next(i for i, t in enumerate(topics) if "node-registered" in t)
    assert deploy_idx < registered_idx
