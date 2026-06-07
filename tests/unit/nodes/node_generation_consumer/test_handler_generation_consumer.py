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
def test_handler_reads_endpoint_ref_from_contract_model_routing(tmp_path: Path) -> None:
    """OMN-12802: the handler reads the bifrost backend_id (endpoint_ref) from the
    contract — the key it resolves the endpoint with — not an endpoint env var."""
    contract = {
        "name": "node_generation_consumer",
        "contract_version": {"major": 1, "minor": 0, "patch": 0},
        "node_type": "orchestrator",
        "node_version": {"major": 1, "minor": 0, "patch": 0},
        "event_bus": {"publish_topics": [], "subscribe_topics": []},
        "model_routing": {
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

    assert handler._endpoint_ref == "local-coder"
    assert handler._served_model_id == "test-model-v1"
    # The env-indirection attributes are gone — resolution is via the authority.
    assert not hasattr(handler, "_endpoint_env")
    assert not hasattr(handler, "_endpoint_mode")


@pytest.mark.unit
def test_production_contract_declares_endpoint_ref_not_env() -> None:
    """OMN-12802: the production contract must declare endpoint_ref (the bifrost
    backend_id) and must NOT carry the shared endpoint env/mode indirection."""
    from omnimarket.nodes.node_generation_consumer.handlers.handler_generation_consumer import (
        _CONTRACT_PATH,
    )

    with open(_CONTRACT_PATH) as f:
        contract = yaml.safe_load(f)

    model_routing = contract.get("model_routing", {})
    assert model_routing.get("endpoint_ref") == "local-coder", (
        "contract.yaml model_routing.endpoint_ref must be the bifrost backend_id; "
        f"got: {model_routing.get('endpoint_ref')!r}"
    )
    assert "endpoint_env" not in model_routing, (
        "endpoint_env (shared LLM_CODER_URL indirection) must be removed (OMN-12802)"
    )
    assert "endpoint_mode" not in model_routing, (
        "endpoint_mode must be removed — the authority's URL is provider-correct"
    )


@pytest.mark.unit
def test_production_contract_drops_llm_coder_url_dependency() -> None:
    """OMN-12802: the LLM_CODER_URL env dependency is removed; the transport
    secret + CIDR allowlist remain (enforced by MixinLlmHttpTransport)."""
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
    assert "LLM_CODER_URL" not in env_keys, (
        "LLM_CODER_URL must be removed — the endpoint is resolved from the routing "
        "authority, not a shared env var (OMN-12802)"
    )
    assert "LLM_CODER_MODEL_NAME" not in env_keys
    # Transport trust-boundary env still required.
    assert {"LOCAL_LLM_SHARED_SECRET", "LLM_ENDPOINT_CIDR_ALLOWLIST"} <= env_keys


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
# OMN-12683/OMN-12802: the endpoint is resolved PER backend from the bifrost
# routing authority (overlay supplies the full provider-correct URL). The final
# POST URL is proven by running the request the handler builds through the real
# infra URL builder (HandlerLlmOpenaiCompatible._build_url), so the assertion is
# end-to-end, not a restatement of handler internals.
# ---------------------------------------------------------------------------

# A COMPLETE Gemini OpenAI-compatible endpoint (ending in /chat/completions). The
# authority supplies it in full; it must POST verbatim — no version path appended.
_GEMINI_FULL_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
)
_GEMINI_EXPECTED_URL = (
    "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
)
# The double-versioned 404 the old shared-base + path-append produced.
_GEMINI_DOUBLE_VERSIONED_BAD_URL = (
    "https://generativelanguage.googleapis.com/v1beta/openai/v1/chat/completions"
)


def _write_endpoint_overlay(tmp_path: Path, backend_id: str, endpoint_url: str) -> Path:
    """Overlay that supplies a full endpoint_url for one backend, mirroring the
    runtime bifrost_overrides.yaml."""
    overlay = {
        "backends": [
            {
                "backend_id": backend_id,
                "endpoint_url": endpoint_url,
                "model_name": "gemini-2.0-flash",
            }
        ]
    }
    path = tmp_path / f"overlay-{backend_id}.yaml"
    path.write_text(yaml.safe_dump(overlay), encoding="utf-8")
    return path


async def _request_for_backend(
    monkeypatch: Any, tmp_path: Path, backend_id: str, endpoint_url: str
) -> Any:
    """Build and capture the request the handler constructs after resolving
    ``backend_id``'s endpoint from the bifrost authority (no env URL)."""
    config_path = (
        Path(__file__).parent.parent.parent.parent.parent
        / "src"
        / "omnimarket"
        / "configs"
        / "bifrost_delegation.yaml"
    )
    overlay = _write_endpoint_overlay(tmp_path, backend_id, endpoint_url)
    monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(config_path))
    monkeypatch.setenv("BIFROST_OVERLAY_PATH", str(overlay))

    contract = {
        "name": "node_generation_consumer",
        "contract_version": {"major": 1, "minor": 0, "patch": 0},
        "node_type": "orchestrator",
        "node_version": {"major": 1, "minor": 0, "patch": 0},
        "event_bus": {"publish_topics": [], "subscribe_topics": []},
        "model_routing": {
            "served_model_id": "gemini-2.0-flash",
            "endpoint_ref": backend_id,
            "provider": "local",
        },
    }
    contract_path = tmp_path / "contract.yaml"
    contract_path.write_text(yaml.dump(contract))

    capturing = _RequestCapturingEffect()
    handler = HandlerGenerationConsumer(
        contract_path=contract_path,
        event_publisher=lambda _t, _p: None,
    )
    handler._effect = capturing
    handler._injected_effect = False

    await handler.handle(
        ModelNodeGenerationRequest(
            task_description="Build a stub node",
            correlation_id="corr-url-1",
        )
    )

    assert capturing.requests, "handler must build a request"
    return capturing.requests[0]


async def _final_post_url_for_backend(
    monkeypatch: Any, tmp_path: Path, backend_id: str, endpoint_url: str
) -> str:
    from omnibase_infra.nodes.node_llm_inference_effect.handlers.handler_llm_openai_compatible import (
        HandlerLlmOpenaiCompatible,
    )

    request = await _request_for_backend(
        monkeypatch, tmp_path, backend_id, endpoint_url
    )
    return HandlerLlmOpenaiCompatible._build_url(request)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_gemini_full_endpoint_posts_as_is(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """A full Gemini endpoint resolved from the authority is POSTed verbatim."""
    final_url = await _final_post_url_for_backend(
        monkeypatch, tmp_path, "cloud-gemini-flash", _GEMINI_FULL_ENDPOINT
    )
    assert final_url == _GEMINI_EXPECTED_URL
    assert final_url != _GEMINI_DOUBLE_VERSIONED_BAD_URL


@pytest.mark.unit
@pytest.mark.asyncio
async def test_gemini_full_endpoint_no_double_versioned_append(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """Regression: full Gemini endpoint must not become /v1beta/openai/v1/chat/completions."""
    final_url = await _final_post_url_for_backend(
        monkeypatch, tmp_path, "cloud-gemini-flash", _GEMINI_FULL_ENDPOINT
    )
    assert "/v1beta/openai/v1/chat/completions" not in final_url
    assert final_url.endswith("/v1beta/openai/chat/completions")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_gemini_request_sets_endpoint_url_field(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """The built request must carry the full resolved endpoint_url."""
    request = await _request_for_backend(
        monkeypatch, tmp_path, "cloud-gemini-flash", _GEMINI_FULL_ENDPOINT
    )
    assert request.endpoint_url == _GEMINI_FULL_ENDPOINT


@pytest.mark.unit
def test_old_base_url_path_would_404_proves_regression() -> None:
    """Documents the defect: feeding the OpenAI-compat Gemini BASE through base_url
    (the pre-fix shared-env behavior) yields the double-versioned 404 URL. The fix
    resolves the COMPLETE endpoint from the authority and routes via endpoint_url."""
    from omnibase_infra.enums import EnumLlmOperationType
    from omnibase_infra.nodes.node_llm_inference_effect.handlers.handler_llm_openai_compatible import (
        HandlerLlmOpenaiCompatible,
    )
    from omnibase_infra.nodes.node_llm_inference_effect.models.model_llm_inference_request import (
        ModelLlmInferenceRequest,
    )

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
async def test_provider_error_remains_typed_failure_not_blank_success(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """A provider call failure must not be reported as a successful (blank) generation.

    The handler swallows the LLM exception into an empty raw output, which then
    fails contract validation — so contract_passed stays False (typed failure
    surface), and no deploy/registration is emitted. A 404 must never look like
    success.
    """
    overlay = _write_endpoint_overlay(tmp_path, "local-coder", _GEMINI_FULL_ENDPOINT)
    config_path = (
        Path(__file__).parent.parent.parent.parent.parent
        / "src"
        / "omnimarket"
        / "configs"
        / "bifrost_delegation.yaml"
    )
    monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(config_path))
    monkeypatch.setenv("BIFROST_OVERLAY_PATH", str(overlay))

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


# ---------------------------------------------------------------------------
# OMN-12802: endpoint resolved from the bifrost routing authority (no env)
# ---------------------------------------------------------------------------

_GEN_VLLM_URL = "http://192.168.86.201:8000/v1/chat/completions"  # onex-allow-internal-ip OMN-12802 reason="test fixture: representative local vLLM endpoint proving the handler resolves the full URL from the routing authority"


def _write_bifrost_overlay(tmp_path: Path) -> Path:
    overlay = {
        "backends": [
            {
                "backend_id": "local-coder",
                "endpoint_url": _GEN_VLLM_URL,
                "model_name": "Qwen3.6-35B-A3B",
            }
        ]
    }
    path = tmp_path / "bifrost_overrides.yaml"
    path.write_text(yaml.safe_dump(overlay), encoding="utf-8")
    return path


def _bifrost_config_path() -> Path:
    return (
        Path(__file__).parent.parent.parent.parent.parent
        / "src"
        / "omnimarket"
        / "configs"
        / "bifrost_delegation.yaml"
    )


class _RequestCapturingEffect:
    """Captures the ModelLlmInferenceRequest the handler builds, then returns valid output."""

    def __init__(self) -> None:
        self.requests: list[Any] = []

    async def handle(self, request: Any) -> _FakeResponse:
        await asyncio.sleep(0)
        self.requests.append(request)
        return _FakeResponse(_VALID_LLM_RESPONSE)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_generation_resolves_full_endpoint_from_routing_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The handler must POST to the FULL provider-correct URL resolved from the
    bifrost authority keyed by endpoint_ref — never a bare base from LLM_CODER_URL."""
    overlay = _write_bifrost_overlay(tmp_path)
    monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(_bifrost_config_path()))
    monkeypatch.setenv("BIFROST_OVERLAY_PATH", str(overlay))
    # The shared env stopgap must be irrelevant — set it to a wrong bare base to
    # prove the handler does NOT read it.
    monkeypatch.setenv("LLM_CODER_URL", "http://wrong-bare-base:9999")

    capturing = _RequestCapturingEffect()
    handler = HandlerGenerationConsumer(effect_handler=capturing)
    # Injected effect path skips real-effect construction but must still resolve
    # the endpoint from the routing authority and pass it on the request.
    handler._injected_effect = False  # force the real resolution branch

    await handler.handle(
        ModelNodeGenerationRequest(
            task_description="Build a stub node",
            correlation_id="corr-endpoint-1",
            max_attempts=1,
        )
    )

    assert capturing.requests, "handler must call the effect with a built request"
    req = capturing.requests[0]
    assert req.endpoint_url == _GEN_VLLM_URL
    assert req.endpoint_url.endswith("/v1/chat/completions")
    assert "wrong-bare-base" not in (req.endpoint_url or "")
    # The served model name comes from the routing authority, not the env.
    assert req.model == "Qwen3.6-35B-A3B"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_generation_fails_closed_when_backend_endpoint_unconfigured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No overlay endpoint for the backend → fail closed (no silent env default)."""
    # Overlay that does NOT fill local-coder's endpoint.
    empty_overlay = tmp_path / "empty_overlay.yaml"
    empty_overlay.write_text("backends: []\n", encoding="utf-8")
    monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(_bifrost_config_path()))
    monkeypatch.setenv("BIFROST_OVERLAY_PATH", str(empty_overlay))
    monkeypatch.setenv("LLM_CODER_URL", "http://wrong-bare-base:9999")

    capturing = _RequestCapturingEffect()
    handler = HandlerGenerationConsumer(effect_handler=capturing)
    handler._injected_effect = False

    result = await handler.handle(
        ModelNodeGenerationRequest(
            task_description="Build a stub node",
            correlation_id="corr-failclosed-1",
            max_attempts=1,
        )
    )

    # Fail closed: the generation must not succeed by silently reading the env.
    assert result.contract_passed is False
    assert not capturing.requests, (
        "handler must NOT build a request when the backend endpoint is unconfigured"
    )


@pytest.mark.unit
def test_generation_handler_does_not_read_llm_coder_url_env() -> None:
    """Source guard: the handler must not READ the shared LLM_*_URL env (AST scan).

    Comments may explain that the env is intentionally not used; what's banned is
    an actual ``os.environ[...]`` / ``os.getenv(...)`` read of a model-endpoint env.
    """
    import ast as _ast

    handler_path = (
        Path(__file__).parent.parent.parent.parent.parent
        / "src"
        / "omnimarket"
        / "nodes"
        / "node_generation_consumer"
        / "handlers"
        / "handler_generation_consumer.py"
    )
    tree = _ast.parse(handler_path.read_text(encoding="utf-8"))
    banned = {"LLM_CODER_URL", "LLM_CODER_MODEL_NAME"}
    string_literals = {
        node.value
        for node in _ast.walk(tree)
        if isinstance(node, _ast.Constant) and isinstance(node.value, str)
    }
    leaked = banned & string_literals
    assert not leaked, (
        f"generation handler must resolve its endpoint from the routing authority, "
        f"not a shared model-endpoint env var; found literal(s): {sorted(leaked)} "
        "(OMN-12802)"
    )
