# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for HandlerAbCompareOrchestrator.

All tests use FakeEffectHandler — no network, no Kafka, no Docker.
Uses a temp models_registry.yaml with stub models to avoid env var dependencies.
The fake satisfies the HandlerLlmOpenaiCompatible interface:
  handle(request: ModelLlmInferenceRequest) -> ModelLlmInferenceResponse
"""

from __future__ import annotations

import ast
import tempfile
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_ab_compare_orchestrator.handlers.handler_ab_compare_orchestrator import (
    HandlerAbCompareOrchestrator,
    _calculate_cost,
    _resolve_models,
    _ResolvedModel,
)
from omnimarket.nodes.node_ab_compare_orchestrator.models.model_ab_compare_start import (
    ModelAbCompareStart,
)

# ---------------------------------------------------------------------------
# Fake result — duck-types ModelLlmInferenceResponse fields the orchestrator reads.
# No omnibase_infra import needed for unit tests.
# ---------------------------------------------------------------------------


class _FakeUsage:
    def __init__(self, prompt: int, completion: int) -> None:
        self.tokens_input = prompt
        self.tokens_output = completion
        self.tokens_total = prompt + completion


class _FakeResponse:
    """Duck-type for ModelLlmInferenceResponse — exposes only fields the orchestrator reads."""

    def __init__(
        self,
        model_id: str,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        latency_ms: float = 0.0,
        generated_text: str = "",
    ) -> None:
        self.model_id = model_id
        self.usage = _FakeUsage(prompt_tokens, completion_tokens)
        self.latency_ms = latency_ms
        self.generated_text = generated_text or None  # None if empty


# ---------------------------------------------------------------------------
# Fake effect handler — satisfies HandlerLlmOpenaiCompatible.handle() signature.
# Keyed by model ID string (request.model == model_id_resolved from registry).
# ---------------------------------------------------------------------------

_FAKE_OUTPUT = (
    "def validate_uuid(s: str) -> bool:\n"
    "    import uuid\n"
    "    try:\n"
    "        uuid.UUID(s)\n"
    "        return True\n"
    "    except ValueError:\n"
    "        return False\n"
)


class FakeEffectHandler:
    """Deterministic fake: returns fixed token counts and output per model resolved ID."""

    def __init__(
        self,
        responses: dict[str, tuple[str, int, int, int]] | None = None,
        error_for: set[str] | None = None,
    ) -> None:
        # responses: model_id_resolved -> (raw_output, prompt_tokens, completion_tokens, latency_ms)
        self._responses = responses or {}
        self._default = (_FAKE_OUTPUT, 50, 100, 500)
        self._error_for = error_for or set()

    async def handle(self, request: Any) -> _FakeResponse:
        # request.model == model_id_resolved (e.g. "stub-model-a")
        model_id: str = request.model

        if model_id in self._error_for:
            raise RuntimeError(f"Simulated error for {model_id}")

        raw_output, prompt_tokens, completion_tokens, latency_ms = self._responses.get(
            model_id, self._default
        )
        return _FakeResponse(
            model_id=model_id,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=float(latency_ms),
            generated_text=raw_output,
        )


# ---------------------------------------------------------------------------
# Test registry fixtures — use direct base-URL endpoint format
# ---------------------------------------------------------------------------

_REGISTRY_TWO_LOCAL = {
    "schema_version": "1.0.0",
    "models": [
        {
            "id": "stub-local-a",
            "display_name": "Stub Local A",
            "endpoint": "http://stub-a:8000",
            "protocol": "openai_compatible",
            "model_id": "stub-model-a",
            "cost_per_1k_input": 0.0,
            "cost_per_1k_output": 0.0,
            "location": "local",
            "context_window": 8192,
        },
        {
            "id": "stub-cloud-b",
            "display_name": "Stub Cloud B",
            "endpoint": "http://stub-b:9000",
            "protocol": "openai_compatible",
            "model_id": "stub-model-b",
            "cost_per_1k_input": 0.003,
            "cost_per_1k_output": 0.015,
            "location": "cloud",
            "requires_key": "STUB_LLM_B_API_KEY",
            "context_window": 200000,
        },
    ],
}

_REGISTRY_WITH_MISSING_KEY = {
    "schema_version": "1.0.0",
    "models": [
        {
            "id": "stub-cloud-no-key",
            "display_name": "Cloud No Key",
            "endpoint": "http://stub-cloud:9000",
            "protocol": "openai_compatible",
            "model_id": "stub-model-cloud",
            "cost_per_1k_input": 0.01,
            "cost_per_1k_output": 0.01,
            "location": "cloud",
            "requires_key": "STUB_MISSING_API_KEY_XYZ",
            "context_window": 100000,
        },
    ],
}


def _write_registry(data: dict) -> str:  # type: ignore[type-arg]
    """Write registry YAML to a temp file, return path."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(data, f)
        return f.name


def _make_handler_with_registry(
    registry_path: str, fake_effect: FakeEffectHandler
) -> HandlerAbCompareOrchestrator:
    """Construct handler with patched registry and injected fake effect."""
    handler = HandlerAbCompareOrchestrator(effect_handler=fake_effect)
    handler._registry = yaml.safe_load(Path(registry_path).read_text())["models"]
    return handler


# ---------------------------------------------------------------------------
# Unit tests: _resolve_models
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resolve_models_skips_missing_endpoint() -> None:
    """Models with no endpoint field should be skipped."""
    registry = [
        {
            "id": "stub-no-endpoint",
            "display_name": "No Endpoint",
            "protocol": "openai_compatible",
            "model_id": "stub-model",
            "cost_per_1k_input": 0.0,
            "cost_per_1k_output": 0.0,
            "location": "local",
            "context_window": 8192,
        }
    ]
    resolved, skipped = _resolve_models(registry, ["all"])  # type: ignore[arg-type]
    assert "stub-no-endpoint" in skipped
    assert len(resolved) == 0


@pytest.mark.unit
def test_resolve_models_skips_missing_requires_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("STUB_LLM_B_API_KEY", raising=False)
    registry = _REGISTRY_TWO_LOCAL["models"]
    resolved, skipped = _resolve_models(registry, ["all"])  # type: ignore[arg-type]
    assert "stub-cloud-b" in skipped
    assert any(r.model_id == "stub-local-a" for r in resolved)


@pytest.mark.unit
def test_resolve_models_filters_to_requested(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STUB_LLM_B_API_KEY", "test-key")
    registry = _REGISTRY_TWO_LOCAL["models"]
    resolved, _skipped = _resolve_models(registry, ["stub-local-a"])  # type: ignore[arg-type]
    assert len(resolved) == 1
    assert resolved[0].model_id == "stub-local-a"


@pytest.mark.unit
def test_resolve_models_uses_model_id_field() -> None:
    """model_id field is used as the resolved model identifier."""
    registry = _REGISTRY_TWO_LOCAL["models"]
    resolved, _ = _resolve_models(registry, ["stub-local-a"])  # type: ignore[arg-type]
    assert resolved[0].model_id_resolved == "stub-model-a"


@pytest.mark.unit
def test_resolve_models_honors_endpoint_and_model_env_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cloud model endpoint/model registry values can be supplied by env."""
    monkeypatch.setenv("STUB_GLM_KEY", "test-key")
    monkeypatch.setenv("STUB_GLM_URL", "https://example.invalid/api/paas/v4")
    monkeypatch.setenv("STUB_GLM_MODEL", "glm-test")
    registry = [
        {
            "id": "stub-glm",
            "display_name": "Stub GLM",
            "endpoint": "https://default.invalid/api/paas/v4",
            "endpoint_env": "STUB_GLM_URL",
            "endpoint_path": "/chat/completions",
            "protocol": "openai_compatible",
            "model_id": "glm-default",
            "model_id_env": "STUB_GLM_MODEL",
            "cost_per_1k_input": 0.0005,
            "cost_per_1k_output": 0.0005,
            "location": "cloud",
            "requires_key": "STUB_GLM_KEY",
            "context_window": 131072,
        }
    ]

    resolved, skipped = _resolve_models(registry, ["all"])  # type: ignore[arg-type]

    assert skipped == []
    assert len(resolved) == 1
    assert resolved[0].endpoint_url == "https://example.invalid/api/paas/v4"
    assert (
        resolved[0].full_endpoint_url
        == "https://example.invalid/api/paas/v4/chat/completions"
    )
    assert resolved[0].model_id_resolved == "glm-test"


@pytest.mark.unit
def test_resolve_models_skips_non_openai_compatible_protocol() -> None:
    """Non openai_compatible protocols are skipped."""
    registry = [
        {
            "id": "stub-anthropic",
            "display_name": "Anthropic",
            "protocol": "anthropic",
            "model_id": "claude-sonnet",
            "cost_per_1k_input": 0.003,
            "cost_per_1k_output": 0.015,
            "location": "cloud",
            "context_window": 200000,
        }
    ]
    resolved, skipped = _resolve_models(registry, ["all"])  # type: ignore[arg-type]
    assert "stub-anthropic" in skipped
    assert len(resolved) == 0


# ---------------------------------------------------------------------------
# Unit tests: _calculate_cost
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_calculate_cost_local_is_zero() -> None:
    model = _ResolvedModel(
        model_id="local-a",
        display_name="Local A",
        endpoint_url="http://local:8000",
        protocol="openai_compatible",
        model_id_resolved="model-a",
        cost_per_1k_input=0.0,
        cost_per_1k_output=0.0,
        location="local",
        context_window=8192,
    )
    assert _calculate_cost(model, prompt_tokens=1000, completion_tokens=500) == 0.0


@pytest.mark.unit
def test_calculate_cost_cloud_correctness() -> None:
    model = _ResolvedModel(
        model_id="cloud-b",
        display_name="Cloud B",
        endpoint_url="http://cloud:9000",
        protocol="openai_compatible",
        model_id_resolved="model-b",
        cost_per_1k_input=0.003,
        cost_per_1k_output=0.015,
        location="cloud",
        context_window=200000,
    )
    # 500 input @ $0.003/1k = $0.0015, 200 output @ $0.015/1k = $0.003 → $0.0045
    cost = _calculate_cost(model, prompt_tokens=500, completion_tokens=200)
    assert abs(cost - 0.0045) < 1e-9


# ---------------------------------------------------------------------------
# Integration-style tests: handler.handle() with fake effect
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_fans_out_to_all_available_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STUB_LLM_B_API_KEY", "test-key")

    fake = FakeEffectHandler()
    reg_path = _write_registry(_REGISTRY_TWO_LOCAL)
    handler = _make_handler_with_registry(reg_path, fake)

    result = await handler.handle(
        ModelAbCompareStart(task="Write a UUID validator", correlation_id="test-corr-1")
    )

    assert len(result.comparison) == 2
    model_keys = {r.model_key for r in result.comparison}
    assert "stub-local-a" in model_keys
    assert "stub-cloud-b" in model_keys
    Path(reg_path).unlink(missing_ok=True)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_local_model_cost_is_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STUB_LLM_B_API_KEY", raising=False)

    fake = FakeEffectHandler()
    reg_path = _write_registry(_REGISTRY_TWO_LOCAL)
    handler = _make_handler_with_registry(reg_path, fake)

    result = await handler.handle(
        ModelAbCompareStart(task="test", correlation_id="corr-zero")
    )

    local_rows = [r for r in result.comparison if r.model_key == "stub-local-a"]
    assert len(local_rows) == 1
    assert local_rows[0].cost_usd == 0.0
    Path(reg_path).unlink(missing_ok=True)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_cloud_cost_calculated_from_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STUB_LLM_B_API_KEY", "test-key")
    # Only cloud model — exclude local by filtering
    fake = FakeEffectHandler()
    reg_path = _write_registry(_REGISTRY_TWO_LOCAL)
    handler = _make_handler_with_registry(reg_path, fake)

    result = await handler.handle(
        ModelAbCompareStart(
            task="test",
            correlation_id="corr-cloud-cost",
            models=["stub-cloud-b"],
        )
    )

    cloud_rows = [r for r in result.comparison if r.model_key == "stub-cloud-b"]
    assert len(cloud_rows) == 1
    # FakeEffectHandler default: 50 prompt, 100 completion tokens
    expected = (50 * 0.003 + 100 * 0.015) / 1000.0
    assert abs(cloud_rows[0].cost_usd - expected) < 1e-9
    Path(reg_path).unlink(missing_ok=True)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_rows_sorted_by_cost_ascending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STUB_LLM_B_API_KEY", "test-key")

    fake = FakeEffectHandler()
    reg_path = _write_registry(_REGISTRY_TWO_LOCAL)
    handler = _make_handler_with_registry(reg_path, fake)

    result = await handler.handle(
        ModelAbCompareStart(task="test", correlation_id="corr-sort")
    )

    costs = [r.cost_usd for r in result.comparison]
    assert costs == sorted(costs)
    Path(reg_path).unlink(missing_ok=True)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_skips_model_with_missing_required_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("STUB_MISSING_API_KEY_XYZ", raising=False)

    fake = FakeEffectHandler()
    reg_path = _write_registry(_REGISTRY_WITH_MISSING_KEY)
    handler = _make_handler_with_registry(reg_path, fake)

    result = await handler.handle(
        ModelAbCompareStart(task="test", correlation_id="corr-skip-key")
    )

    assert "stub-cloud-no-key" in result.models_skipped
    assert len(result.comparison) == 0
    Path(reg_path).unlink(missing_ok=True)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_specific_model_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STUB_LLM_B_API_KEY", "test-key")

    fake = FakeEffectHandler()
    reg_path = _write_registry(_REGISTRY_TWO_LOCAL)
    handler = _make_handler_with_registry(reg_path, fake)

    result = await handler.handle(
        ModelAbCompareStart(
            task="test", correlation_id="corr-specific", models=["stub-local-a"]
        )
    )

    assert len(result.comparison) == 1
    assert result.comparison[0].model_key == "stub-local-a"
    Path(reg_path).unlink(missing_ok=True)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_status_partial_when_some_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("STUB_LLM_B_API_KEY", raising=False)

    fake = FakeEffectHandler()
    reg_path = _write_registry(_REGISTRY_TWO_LOCAL)
    handler = _make_handler_with_registry(reg_path, fake)

    result = await handler.handle(
        ModelAbCompareStart(task="test", correlation_id="corr-partial")
    )

    assert result.status == "PARTIAL"
    assert "stub-cloud-b" in result.models_skipped
    Path(reg_path).unlink(missing_ok=True)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_status_completed_when_all_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STUB_LLM_B_API_KEY", "test-key")

    fake = FakeEffectHandler()
    reg_path = _write_registry(_REGISTRY_TWO_LOCAL)
    handler = _make_handler_with_registry(reg_path, fake)

    result = await handler.handle(
        ModelAbCompareStart(task="test", correlation_id="corr-complete")
    )

    assert result.status == "COMPLETED"
    assert not result.models_skipped
    Path(reg_path).unlink(missing_ok=True)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_records_token_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STUB_LLM_B_API_KEY", raising=False)

    # Key by model_id_resolved ("stub-model-a") — that's what request.model is set to
    fake = FakeEffectHandler(responses={"stub-model-a": (_FAKE_OUTPUT, 42, 88, 300)})
    reg_path = _write_registry(_REGISTRY_TWO_LOCAL)
    handler = _make_handler_with_registry(reg_path, fake)

    result = await handler.handle(
        ModelAbCompareStart(task="test", correlation_id="corr-tokens")
    )

    row = result.comparison[0]
    assert row.prompt_tokens == 42
    assert row.completion_tokens == 88
    assert row.total_tokens == 130
    Path(reg_path).unlink(missing_ok=True)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_effect_exception_recorded_in_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Effect handler exceptions are captured into the row error field, not raised."""
    monkeypatch.delenv("STUB_LLM_B_API_KEY", raising=False)

    # Key by model_id_resolved ("stub-model-a") — that's what request.model is set to
    fake = FakeEffectHandler(error_for={"stub-model-a"})
    reg_path = _write_registry(_REGISTRY_TWO_LOCAL)
    handler = _make_handler_with_registry(reg_path, fake)

    result = await handler.handle(
        ModelAbCompareStart(task="test", correlation_id="corr-effect-error")
    )

    assert len(result.comparison) == 1
    assert result.comparison[0].model_key == "stub-local-a"
    assert "Simulated error" in result.comparison[0].error
    Path(reg_path).unlink(missing_ok=True)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_no_httpx_or_health_probe_in_orchestrator() -> None:
    """Verify the orchestrator module does not import httpx or do health probes."""
    import importlib
    import sys

    mod_name = "omnimarket.nodes.node_ab_compare_orchestrator.handlers.handler_ab_compare_orchestrator"
    if mod_name in sys.modules:
        mod = sys.modules[mod_name]
    else:
        mod = importlib.import_module(mod_name)
    source_path = Path(mod.__file__ or "")
    source = source_path.read_text()
    tree = ast.parse(source)

    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(
                alias.name.split(".", maxsplit=1)[0] for alias in node.names
            )
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)

    assert "httpx" not in imported_modules
    assert not any("node_ab_inference_effect" in name for name in imported_modules)

    forbidden_symbols = {
        "ProtocolAbLlmClient",
        "_probe_health",
        "_DefaultHttpLlmClient",
        "ProtocolInferenceEffect",
    }
    for symbol in forbidden_symbols:
        assert symbol not in source
