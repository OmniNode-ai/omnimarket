# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for HandlerGenerationConsumer.

All tests use FakeLlmEffect — no network, no Kafka, no Docker.
The fake satisfies the HandlerLlmOpenaiCompatible interface:
    async def handle(request) -> response
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

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
        await asyncio.sleep(0)
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
    handler_with_path = 'def handle(x):\n    return "/Users/foo/bar"\n'  # test-literal-ok: testing validator rejects hardcoded paths
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


@pytest.mark.unit
def test_validate_generation_fails_on_empty_handler() -> None:
    result = _validate_generation(_VALID_CONTRACT_YAML, "")
    assert result["valid"] is False
    assert any("empty" in e for e in result["errors"])


@pytest.mark.unit
def test_validate_generation_fails_when_handle_function_missing() -> None:
    handler_no_handle = "def process(x):\n    return x\n"
    result = _validate_generation(_VALID_CONTRACT_YAML, handler_no_handle)
    assert result["valid"] is False
    assert any("handle()" in e for e in result["errors"])


@pytest.mark.unit
@pytest.mark.asyncio
async def test_registration_not_emitted_when_deploy_publisher_raises() -> None:
    """Registration must be suppressed when deploy publish fails."""
    published: list[tuple[str, bytes]] = []

    def _failing_publisher(topic: str, payload: bytes) -> None:
        if "node-deploy" in topic:
            raise RuntimeError("broker down")
        published.append((topic, payload))

    handler = _make_handler([_VALID_LLM_RESPONSE], published=None)
    handler._event_publisher = _failing_publisher

    await handler.handle(
        ModelNodeGenerationRequest(
            task_description="Build a stub node",
            correlation_id="corr-deploy-fail-gate-1",
        )
    )

    topics = [t for t, _ in published]
    assert not any("node-registered" in t for t in topics)


# ---------------------------------------------------------------------------
# Contract model_routing endpoint resolution tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_handler_reads_endpoint_env_from_contract_model_routing(tmp_path: Path) -> None:
    """Handler must derive the endpoint env var name and mode from contract.yaml model_routing."""
    contract = {
        "name": "node_generation_consumer",
        "contract_version": {"major": 1, "minor": 0, "patch": 0},
        "node_type": "orchestrator",
        "node_version": {"major": 1, "minor": 0, "patch": 0},
        "event_bus": {"publish_topics": [], "subscribe_topics": []},
        "model_routing": {
            "endpoint_env": "LLM_CODER_URL",
            "endpoint_mode": "complete_endpoint",
            "served_model_id": "test-model-v1",
            "endpoint_ref": "local-coder",
            "provider": "local",
        },
    }
    contract_path = tmp_path / "contract.yaml"
    contract_path.write_text(yaml.dump(contract))

    handler = HandlerGenerationConsumer(
        effect_handler=FakeLlmEffect([_VALID_LLM_RESPONSE]),
        contract_path=contract_path,
    )

    assert handler._endpoint_env == "LLM_CODER_URL"
    assert handler._endpoint_mode == "complete_endpoint"
    # OMN-12779: no _model_id_env — model ID is read directly from served_model_id.
    assert handler._served_model_id == "test-model-v1"


@pytest.mark.unit
def test_handler_rejects_contract_without_valid_endpoint_mode(tmp_path: Path) -> None:
    """Endpoint request shape must be explicitly declared by the contract."""
    contract = {
        "name": "node_generation_consumer",
        "contract_version": {"major": 1, "minor": 0, "patch": 0},
        "node_type": "orchestrator",
        "node_version": {"major": 1, "minor": 0, "patch": 0},
        "event_bus": {"publish_topics": [], "subscribe_topics": []},
        "model_routing": {
            "endpoint_env": "LLM_CODER_URL",
            "served_model_id": "test-model-v1",
            "endpoint_ref": "local-coder",
            "provider": "local",
            # endpoint_mode intentionally absent
        },
    }
    contract_path = tmp_path / "contract.yaml"
    contract_path.write_text(yaml.dump(contract))

    with pytest.raises(ValueError, match="endpoint_mode must be one of"):
        HandlerGenerationConsumer(
            effect_handler=FakeLlmEffect([_VALID_LLM_RESPONSE]),
            contract_path=contract_path,
        )


@pytest.mark.unit
def test_production_contract_declares_llm_coder_url_endpoint_env() -> None:
    """The production contract.yaml must declare model_routing.endpoint_env=LLM_CODER_URL."""
    from omnimarket.nodes.node_generation_consumer.handlers.handler_generation_consumer import (
        _CONTRACT_PATH,
    )

    with open(_CONTRACT_PATH) as f:
        contract = yaml.safe_load(f)

    model_routing = contract.get("model_routing", {})
    assert model_routing.get("endpoint_env") == "LLM_CODER_URL", (
        "contract.yaml model_routing.endpoint_env must be 'LLM_CODER_URL'; "
        f"got: {model_routing.get('endpoint_env')!r}"
    )
    assert model_routing.get("endpoint_mode") == "complete_endpoint", (
        "contract.yaml model_routing.endpoint_mode must be 'complete_endpoint'; "
        f"got: {model_routing.get('endpoint_mode')!r}"
    )


@pytest.mark.unit
def test_production_contract_declares_required_env_dependencies() -> None:
    """contract.yaml must declare LLM_CODER_URL, LOCAL_LLM_SHARED_SECRET, LLM_ENDPOINT_CIDR_ALLOWLIST as dependencies."""
    from omnimarket.nodes.node_generation_consumer.handlers.handler_generation_consumer import (
        _CONTRACT_PATH,
    )

    with open(_CONTRACT_PATH) as f:
        contract = yaml.safe_load(f)

    env_keys = {
        dep["key"]
        for dep in contract.get("dependencies", [])
        if isinstance(dep, dict) and dep.get("type") == "environment" and "key" in dep
    }
    # LLM_CODER_MODEL_NAME removed from required: served_model_id is now declared
    # directly in model_routing.served_model_id (not resolved via env indirection).
    required = {
        "LLM_CODER_URL",
        "LOCAL_LLM_SHARED_SECRET",
        "LLM_ENDPOINT_CIDR_ALLOWLIST",
    }
    missing = required - env_keys
    assert not missing, (
        f"contract.yaml is missing environment dependency declarations for: {missing}"
    )


# ---------------------------------------------------------------------------
# OMN-12779: model/provider/endpoint from contract, not env (Wave 1B)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_production_contract_declares_provider() -> None:
    """contract.yaml model_routing.provider must be non-empty (no env fallback)."""
    from omnimarket.nodes.node_generation_consumer.handlers.handler_generation_consumer import (
        _CONTRACT_PATH,
    )

    with open(_CONTRACT_PATH) as f:
        contract = yaml.safe_load(f)

    model_routing = contract.get("model_routing", {})
    provider = model_routing.get("provider", "")
    assert provider, (
        f"contract.yaml model_routing.provider is required; got: {provider!r}"
    )


@pytest.mark.unit
def test_production_contract_declares_served_model_id() -> None:
    """contract.yaml model_routing.served_model_id must be declared directly, not via env."""
    from omnimarket.nodes.node_generation_consumer.handlers.handler_generation_consumer import (
        _CONTRACT_PATH,
    )

    with open(_CONTRACT_PATH) as f:
        contract = yaml.safe_load(f)

    model_routing = contract.get("model_routing", {})
    served_model_id = model_routing.get("served_model_id", "")
    assert served_model_id, (
        "contract.yaml model_routing.served_model_id must be a non-empty string; "
        "served model IDs must come from the contract/overlay, not from env var indirection. "
        f"got: {served_model_id!r}"
    )
    # The old env-indirection key must be absent — no silent fallback path.
    assert "served_model_id_env" not in model_routing, (
        "contract.yaml must not contain served_model_id_env; "
        "model ID is now declared directly as served_model_id"
    )


@pytest.mark.unit
def test_production_contract_declares_endpoint_ref() -> None:
    """contract.yaml model_routing.endpoint_ref must reference a routing-tier backend."""
    from omnimarket.nodes.node_generation_consumer.handlers.handler_generation_consumer import (
        _CONTRACT_PATH,
    )

    with open(_CONTRACT_PATH) as f:
        contract = yaml.safe_load(f)

    model_routing = contract.get("model_routing", {})
    endpoint_ref = model_routing.get("endpoint_ref", "")
    assert endpoint_ref, (
        "contract.yaml model_routing.endpoint_ref is required; "
        "it must reference a routing-tier backend (e.g. 'local-coder'). "
        f"got: {endpoint_ref!r}"
    )


@pytest.mark.unit
def test_handler_reads_provider_from_contract(tmp_path: Path) -> None:
    """Handler must read model_routing.provider from contract, fail-fast if absent."""
    contract = {
        "name": "node_generation_consumer",
        "contract_version": {"major": 1, "minor": 0, "patch": 0},
        "node_type": "orchestrator",
        "node_version": {"major": 1, "minor": 0, "patch": 0},
        "event_bus": {"publish_topics": [], "subscribe_topics": []},
        "model_routing": {
            "endpoint_env": "LLM_CODER_URL",
            "endpoint_mode": "complete_endpoint",
            "served_model_id": "test-model-v1",
            "endpoint_ref": "local-coder",
            "provider": "local",
        },
    }
    contract_path = tmp_path / "contract.yaml"
    contract_path.write_text(yaml.dump(contract))

    handler = HandlerGenerationConsumer(
        effect_handler=FakeLlmEffect([_VALID_LLM_RESPONSE]),
        contract_path=contract_path,
    )

    assert handler._provider == "local"


@pytest.mark.unit
def test_handler_rejects_contract_without_provider(tmp_path: Path) -> None:
    """Handler must raise ValueError when model_routing.provider is absent."""
    contract = {
        "name": "node_generation_consumer",
        "contract_version": {"major": 1, "minor": 0, "patch": 0},
        "node_type": "orchestrator",
        "node_version": {"major": 1, "minor": 0, "patch": 0},
        "event_bus": {"publish_topics": [], "subscribe_topics": []},
        "model_routing": {
            "endpoint_env": "LLM_CODER_URL",
            "endpoint_mode": "complete_endpoint",
            "served_model_id": "test-model-v1",
            "endpoint_ref": "local-coder",
            # provider intentionally absent
        },
    }
    contract_path = tmp_path / "contract.yaml"
    contract_path.write_text(yaml.dump(contract))

    with pytest.raises(ValueError, match=r"model_routing\.provider is required"):
        HandlerGenerationConsumer(
            effect_handler=FakeLlmEffect([_VALID_LLM_RESPONSE]),
            contract_path=contract_path,
        )


@pytest.mark.unit
def test_handler_reads_served_model_id_from_contract(tmp_path: Path) -> None:
    """Handler must read model_routing.served_model_id directly from contract."""
    contract = {
        "name": "node_generation_consumer",
        "contract_version": {"major": 1, "minor": 0, "patch": 0},
        "node_type": "orchestrator",
        "node_version": {"major": 1, "minor": 0, "patch": 0},
        "event_bus": {"publish_topics": [], "subscribe_topics": []},
        "model_routing": {
            "endpoint_env": "LLM_CODER_URL",
            "endpoint_mode": "complete_endpoint",
            "served_model_id": "Qwen3.6-35B-A3B",
            "endpoint_ref": "local-coder",
            "provider": "local",
        },
    }
    contract_path = tmp_path / "contract.yaml"
    contract_path.write_text(yaml.dump(contract))

    handler = HandlerGenerationConsumer(
        effect_handler=FakeLlmEffect([_VALID_LLM_RESPONSE]),
        contract_path=contract_path,
    )

    assert handler._served_model_id == "Qwen3.6-35B-A3B"


@pytest.mark.unit
def test_handler_rejects_contract_without_served_model_id(tmp_path: Path) -> None:
    """Handler must raise ValueError when model_routing.served_model_id is absent."""
    contract = {
        "name": "node_generation_consumer",
        "contract_version": {"major": 1, "minor": 0, "patch": 0},
        "node_type": "orchestrator",
        "node_version": {"major": 1, "minor": 0, "patch": 0},
        "event_bus": {"publish_topics": [], "subscribe_topics": []},
        "model_routing": {
            "endpoint_env": "LLM_CODER_URL",
            "endpoint_mode": "complete_endpoint",
            "endpoint_ref": "local-coder",
            "provider": "local",
            # served_model_id intentionally absent
        },
    }
    contract_path = tmp_path / "contract.yaml"
    contract_path.write_text(yaml.dump(contract))

    with pytest.raises(ValueError, match=r"model_routing\.served_model_id is required"):
        HandlerGenerationConsumer(
            effect_handler=FakeLlmEffect([_VALID_LLM_RESPONSE]),
            contract_path=contract_path,
        )


@pytest.mark.unit
def test_handler_rejects_contract_without_endpoint_ref(tmp_path: Path) -> None:
    """Handler must raise ValueError when model_routing.endpoint_ref is absent."""
    contract = {
        "name": "node_generation_consumer",
        "contract_version": {"major": 1, "minor": 0, "patch": 0},
        "node_type": "orchestrator",
        "node_version": {"major": 1, "minor": 0, "patch": 0},
        "event_bus": {"publish_topics": [], "subscribe_topics": []},
        "model_routing": {
            "endpoint_env": "LLM_CODER_URL",
            "endpoint_mode": "complete_endpoint",
            "served_model_id": "test-model-v1",
            "provider": "local",
            # endpoint_ref intentionally absent
        },
    }
    contract_path = tmp_path / "contract.yaml"
    contract_path.write_text(yaml.dump(contract))

    with pytest.raises(ValueError, match=r"model_routing\.endpoint_ref is required"):
        HandlerGenerationConsumer(
            effect_handler=FakeLlmEffect([_VALID_LLM_RESPONSE]),
            contract_path=contract_path,
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_benchmark_uses_provider_and_model_id_from_contract(
    tmp_path: Path,
) -> None:
    """The emitted benchmark must carry provider and model_id sourced from contract, not literals."""
    contract = {
        "name": "node_generation_consumer",
        "contract_version": {"major": 1, "minor": 0, "patch": 0},
        "node_type": "orchestrator",
        "node_version": {"major": 1, "minor": 0, "patch": 0},
        "event_bus": {
            "publish_topics": [
                "onex.evt.omnimarket.node-generation-completed.v1",
                "onex.evt.omnimarket.node-generation-failed.v1",
                "onex.evt.omnimarket.node-registered.v1",
                "onex.cmd.omnimarket.node-deploy.v1",
            ],
            "subscribe_topics": [],
        },
        "model_routing": {
            "endpoint_env": "LLM_CODER_URL",
            "endpoint_mode": "complete_endpoint",
            "served_model_id": "contract-declared-model",
            "endpoint_ref": "local-coder",
            "provider": "contract-declared-provider",
        },
    }
    contract_path = tmp_path / "contract.yaml"
    contract_path.write_text(yaml.dump(contract))

    published: list[tuple[str, bytes]] = []
    handler = HandlerGenerationConsumer(
        effect_handler=FakeLlmEffect([_VALID_LLM_RESPONSE]),
        event_publisher=lambda t, p: published.append((t, p)),
        contract_path=contract_path,
    )

    result = await handler.handle(
        ModelNodeGenerationRequest(
            task_description="Build a stub node",
            correlation_id="corr-contract-provider-1",
        )
    )

    # Both provider and model_id must come from the contract, not literals.
    assert result.provider == "contract-declared-provider"
    assert result.model_id == "contract-declared-model"
    assert result.endpoint_class == "local-coder"

    # Per-attempt records must also carry the contract-sourced values.
    for attempt in result.attempts:
        assert attempt.provider == "contract-declared-provider"
        assert attempt.model_id == "contract-declared-model"
        assert attempt.endpoint_class == "local-coder"


@pytest.mark.unit
def test_handler_routing_source_is_contract(tmp_path: Path) -> None:
    """Handler must expose the routing source as contract-sourced (not env/literal)."""
    contract = {
        "name": "node_generation_consumer",
        "contract_version": {"major": 1, "minor": 0, "patch": 0},
        "node_type": "orchestrator",
        "node_version": {"major": 1, "minor": 0, "patch": 0},
        "event_bus": {"publish_topics": [], "subscribe_topics": []},
        "model_routing": {
            "endpoint_env": "LLM_CODER_URL",
            "endpoint_mode": "complete_endpoint",
            "served_model_id": "Qwen3.6-35B-A3B",
            "endpoint_ref": "local-coder",
            "provider": "local",
        },
    }
    contract_path = tmp_path / "contract.yaml"
    contract_path.write_text(yaml.dump(contract))

    handler = HandlerGenerationConsumer(
        effect_handler=FakeLlmEffect([_VALID_LLM_RESPONSE]),
        contract_path=contract_path,
    )

    assert handler._routing_source == "contract"


# ---------------------------------------------------------------------------
# OMN-12683: endpoint URL preservation is contract-declared. Complete
# endpoints POST as-is via endpoint_url; OpenAI-compatible base URLs keep append.
#
# The final POST URL is proven by running the request the handler builds
# through the *real* infra URL builder (HandlerLlmOpenaiCompatible._build_url),
# so the assertion is end-to-end, not a restatement of handler internals.
# ---------------------------------------------------------------------------

# The contract/overlay supplies the COMPLETE Gemini OpenAI-compatible endpoint
# (ending in /chat/completions). It must be POSTed verbatim — not have any
# version path appended. This is the registered endpoint per the live-path plan.
_GEMINI_FULL_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
)
_GEMINI_EXPECTED_URL = (
    "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
)
# The two 404-producing variants the old base_url-append path created.
_GEMINI_DOUBLE_VERSIONED_BAD_URL = (
    "https://generativelanguage.googleapis.com/v1beta/openai/v1/chat/completions"
)
_GEMINI_ORIGIN_ONLY = "https://generativelanguage.googleapis.com"
_GEMINI_ORIGIN_BAD_URL = "https://generativelanguage.googleapis.com/v1/chat/completions"
_LOCAL_ORIGIN_ONLY = "http://100.109.203.94:8000"
_LOCAL_EXPECTED_URL = "http://100.109.203.94:8000/v1/chat/completions"


def _write_contract_for_endpoint_mode(tmp_path: Path, endpoint_mode: str) -> Path:
    # OMN-12779: served_model_id is declared directly, not via env indirection.
    contract = {
        "name": "node_generation_consumer",
        "contract_version": {"major": 1, "minor": 0, "patch": 0},
        "node_type": "orchestrator",
        "node_version": {"major": 1, "minor": 0, "patch": 0},
        "event_bus": {"publish_topics": [], "subscribe_topics": []},
        "model_routing": {
            "endpoint_env": "LLM_CODER_URL",
            "endpoint_mode": endpoint_mode,
            "served_model_id": "gemini-2.0-flash",
            "endpoint_ref": "local-coder",
            "provider": "local",
        },
    }
    contract_path = tmp_path / f"contract-{endpoint_mode}.yaml"
    contract_path.write_text(yaml.dump(contract))
    return contract_path


class _CapturingEffect:
    """Captures the ModelLlmInferenceRequest the handler builds, returns valid."""

    def __init__(self) -> None:
        self.captured: Any | None = None

    async def handle(self, request: Any) -> _FakeResponse:
        await asyncio.sleep(0)
        self.captured = request
        return _FakeResponse(_VALID_LLM_RESPONSE)


async def _request_for_endpoint(
    monkeypatch: Any, tmp_path: Path, endpoint: str, endpoint_mode: str
) -> Any:
    """Build and capture the handler request for ``endpoint``.

    Forces the non-injected code path (so the real ModelLlmInferenceRequest is
    constructed) while capturing it.

    OMN-12779: model ID comes from contract.served_model_id, not from env var.
    Only the endpoint URL env var (LLM_CODER_URL) is still set via overlay.
    """
    monkeypatch.setenv("LLM_CODER_URL", endpoint)

    capturing = _CapturingEffect()
    handler = HandlerGenerationConsumer(
        contract_path=_write_contract_for_endpoint_mode(tmp_path, endpoint_mode),
        event_publisher=lambda _t, _p: None,
    )
    # Use the capturing effect but keep the real request-building branch.
    handler._effect = capturing
    handler._injected_effect = False

    await handler.handle(
        ModelNodeGenerationRequest(
            task_description="Build a stub node",
            correlation_id="corr-url-1",
        )
    )

    assert capturing.captured is not None
    return capturing.captured


async def _final_post_url_for_endpoint(
    monkeypatch: Any, tmp_path: Path, endpoint: str, endpoint_mode: str
) -> str:
    from omnibase_infra.nodes.node_llm_inference_effect.handlers.handler_llm_openai_compatible import (
        HandlerLlmOpenaiCompatible,
    )

    request = await _request_for_endpoint(
        monkeypatch, tmp_path, endpoint, endpoint_mode
    )
    return HandlerLlmOpenaiCompatible._build_url(request)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_gemini_full_endpoint_posts_as_is(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """Full Gemini endpoint routes via endpoint_url — posted verbatim + /chat/completions."""
    final_url = await _final_post_url_for_endpoint(
        monkeypatch,
        tmp_path,
        _GEMINI_FULL_ENDPOINT,
        "complete_endpoint",
    )
    assert final_url == _GEMINI_EXPECTED_URL
    # Neither 404 variant may appear.
    assert final_url != _GEMINI_DOUBLE_VERSIONED_BAD_URL
    assert final_url != _GEMINI_ORIGIN_BAD_URL


@pytest.mark.unit
@pytest.mark.asyncio
async def test_gemini_full_endpoint_no_double_versioned_append(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """Regression: full Gemini endpoint must not become /v1beta/openai/v1/chat/completions."""
    final_url = await _final_post_url_for_endpoint(
        monkeypatch,
        tmp_path,
        _GEMINI_FULL_ENDPOINT,
        "complete_endpoint",
    )
    assert "/v1beta/openai/v1/chat/completions" not in final_url
    assert final_url.endswith("/v1beta/openai/chat/completions")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_gemini_request_sets_endpoint_url_field(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """The built request must carry endpoint_url for the full Gemini endpoint."""
    request = await _request_for_endpoint(
        monkeypatch,
        tmp_path,
        _GEMINI_FULL_ENDPOINT,
        "complete_endpoint",
    )
    assert request.endpoint_url == _GEMINI_FULL_ENDPOINT


@pytest.mark.unit
def test_old_base_url_path_would_404_proves_regression() -> None:
    """Documents the defect: the old code passed the full Gemini URL via base_url.

    Feeding the complete Gemini endpoint through base_url (the pre-fix behavior)
    yields the double-versioned 404 URL. This asserts the broken shape so the
    fix (routing via endpoint_url) is demonstrably different.
    """
    from omnibase_infra.enums import EnumLlmOperationType
    from omnibase_infra.nodes.node_llm_inference_effect.handlers.handler_llm_openai_compatible import (
        HandlerLlmOpenaiCompatible,
    )
    from omnibase_infra.nodes.node_llm_inference_effect.models.model_llm_inference_request import (
        ModelLlmInferenceRequest,
    )

    # The pre-fix overlay supplied the OpenAI-compat base "/v1beta/openai/".
    # Routed through base_url, the legacy append produced the double-versioned
    # 404 URL. The fix configures the COMPLETE endpoint + routes via endpoint_url.
    gemini_openai_base = "https://generativelanguage.googleapis.com/v1beta/openai/"
    old_request = ModelLlmInferenceRequest(
        base_url=gemini_openai_base,
        operation_type=EnumLlmOperationType.CHAT_COMPLETION,
        model="gemini-2.0-flash",
        messages=({"role": "user", "content": "hi"},),
    )
    bad_url = HandlerLlmOpenaiCompatible._build_url(old_request)
    assert bad_url == _GEMINI_DOUBLE_VERSIONED_BAD_URL
    assert bad_url != _GEMINI_EXPECTED_URL


@pytest.mark.unit
@pytest.mark.asyncio
async def test_local_origin_only_keeps_legacy_append(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """Origin-only base URL keeps legacy base_url + /v1/chat/completions append."""
    final_url = await _final_post_url_for_endpoint(
        monkeypatch,
        tmp_path,
        _LOCAL_ORIGIN_ONLY,
        "openai_compatible_base",
    )
    assert final_url == _LOCAL_EXPECTED_URL


@pytest.mark.unit
@pytest.mark.asyncio
async def test_local_origin_only_does_not_set_endpoint_url(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """Origin-only base URL must NOT set endpoint_url (legacy append branch)."""
    request = await _request_for_endpoint(
        monkeypatch,
        tmp_path,
        _LOCAL_ORIGIN_ONLY,
        "openai_compatible_base",
    )
    assert request.endpoint_url is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_provider_error_remains_typed_failure_not_blank_success(
    monkeypatch: Any,
) -> None:
    """A provider call failure must not be reported as a successful (blank) generation.

    The handler swallows the LLM exception into an empty raw output, which then
    fails contract validation — so contract_passed stays False (typed failure
    surface), and no deploy/registration is emitted. A 404 must never look like
    success.
    """
    # OMN-12779: model ID comes from the production contract.yaml (served_model_id),
    # not from LLM_CODER_MODEL_NAME env var. Only the URL env var is needed.
    monkeypatch.setenv("LLM_CODER_URL", _GEMINI_FULL_ENDPOINT)

    class _Failing404Effect:
        async def handle(self, request: Any) -> _FakeResponse:
            await asyncio.sleep(0)
            raise RuntimeError("404 Not Found from provider")

    published: list[tuple[str, bytes]] = []
    handler = HandlerGenerationConsumer(
        event_publisher=lambda t, p: published.append((t, p))
    )
    handler._effect = _Failing404Effect()
    handler._injected_effect = False

    result = await handler.handle(
        ModelNodeGenerationRequest(
            task_description="Build a stub node",
            correlation_id="corr-404-1",
            max_attempts=1,
        )
    )

    assert result.contract_passed is False
    topics = [t for t, _ in published]
    assert any("generation-failed" in t for t in topics)
    assert not any("node-deploy" in t for t in topics)
    assert not any("node-registered" in t for t in topics)
