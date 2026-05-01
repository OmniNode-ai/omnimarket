# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for HandlerAbCompareOrchestrator.

All tests use fakes: no network, no Kafka, no Docker.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pytest
import yaml

from omnimarket.nodes.node_ab_compare_orchestrator.handlers import (
    handler_ab_compare_orchestrator as handler_module,
)
from omnimarket.nodes.node_ab_compare_orchestrator.models.model_ab_compare_start import (
    ModelAbCompareStart,
)

_FAKE_OUTPUT = "def validate_uuid(s: str) -> bool:\n    import uuid\n    try:\n        uuid.UUID(s)\n        return True\n    except ValueError:\n        return False\n"


class FakeLlmClient:
    """Deterministic fake: returns fixed tokens and output per model."""

    def __init__(
        self, responses: dict[str, tuple[str, int, int, int]] | None = None
    ) -> None:
        self._responses = responses or {}
        self._default = (_FAKE_OUTPUT, 50, 100, 500)

    async def call(
        self,
        *,
        endpoint_url: str,
        model_id: str,
        protocol: str,
        system_prompt: str,
        user_prompt: str,
        timeout_seconds: float,
        api_key: str | None,
    ) -> tuple[str, int, int, int]:
        return self._responses.get(model_id, self._default)


_REGISTRY_TWO_LOCAL: dict[str, Any] = {
    "schema_version": "1.0.0",
    "models": [
        {
            "id": "stub-local-a",
            "display_name": "Stub Local A",
            "endpoint_env": "STUB_LLM_A_URL",
            "endpoint_path": "/v1/chat/completions",
            "health_path": "/health",
            "protocol": "openai_compatible",
            "model_id_env": "STUB_LLM_A_MODEL",
            "model_id_default": "stub-model-a",
            "cost_per_1k_input": 0.0,
            "cost_per_1k_output": 0.0,
            "location": "local",
            "context_window": 8192,
        },
        {
            "id": "stub-cloud-b",
            "display_name": "Stub Cloud B",
            "endpoint_env": "STUB_LLM_B_URL",
            "endpoint_path": "/v1/chat/completions",
            "protocol": "openai_compatible",
            "model_id_default": "stub-model-b",
            "cost_per_1k_input": 0.003,
            "cost_per_1k_output": 0.015,
            "location": "cloud",
            "requires_key": "STUB_LLM_B_API_KEY",
            "context_window": 200000,
        },
    ],
}

_REGISTRY_WITH_MISSING_KEY: dict[str, Any] = {
    "schema_version": "1.0.0",
    "models": [
        {
            "id": "stub-cloud-no-key",
            "display_name": "Cloud No Key",
            "endpoint_env": "STUB_CLOUD_URL",
            "endpoint_path": "/v1/chat/completions",
            "protocol": "openai_compatible",
            "model_id_default": "stub-model-cloud",
            "cost_per_1k_input": 0.01,
            "cost_per_1k_output": 0.01,
            "location": "cloud",
            "requires_key": "STUB_MISSING_API_KEY_XYZ",
            "context_window": 100000,
        },
    ],
}


def _models(data: dict[str, Any]) -> list[dict[str, Any]]:
    return cast(list[dict[str, Any]], data["models"])


def _write_registry(data: dict[str, Any]) -> str:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(data, f)
        return f.name


def _make_handler_with_registry(
    registry_path: str, fake_client: FakeLlmClient
) -> handler_module.HandlerAbCompareOrchestrator:
    handler = handler_module.HandlerAbCompareOrchestrator(llm_client=fake_client)
    handler._registry = yaml.safe_load(Path(registry_path).read_text())["models"]
    return handler


@pytest.mark.unit
def test_resolve_models_skips_missing_endpoint_env() -> None:
    resolved, skipped = handler_module._resolve_models(
        _models(_REGISTRY_TWO_LOCAL), ["all"]
    )

    assert "stub-local-a" in skipped
    assert all(r.model_id != "stub-local-a" for r in resolved)


@pytest.mark.unit
def test_resolve_models_skips_missing_requires_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STUB_LLM_A_URL", "http://stub-a:8000")
    monkeypatch.delenv("STUB_LLM_B_API_KEY", raising=False)

    resolved, skipped = handler_module._resolve_models(
        _models(_REGISTRY_TWO_LOCAL), ["all"]
    )

    assert "stub-cloud-b" in skipped
    assert any(r.model_id == "stub-local-a" for r in resolved)


@pytest.mark.unit
def test_resolve_models_filters_to_requested(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STUB_LLM_A_URL", "http://stub-a:8000")
    monkeypatch.setenv("STUB_LLM_B_URL", "http://stub-b:9000")
    monkeypatch.setenv("STUB_LLM_B_API_KEY", "test-key")

    resolved, _skipped = handler_module._resolve_models(
        _models(_REGISTRY_TWO_LOCAL), ["stub-local-a"]
    )

    assert len(resolved) == 1
    assert resolved[0].model_id == "stub-local-a"


@pytest.mark.unit
def test_resolve_models_uses_model_id_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STUB_LLM_A_URL", "http://stub-a:8000")
    monkeypatch.setenv("STUB_LLM_A_MODEL", "overridden-model-id")

    resolved, _ = handler_module._resolve_models(
        _models(_REGISTRY_TWO_LOCAL), ["stub-local-a"]
    )

    assert resolved[0].model_id_resolved == "overridden-model-id"


@pytest.mark.unit
def test_resolve_models_preserves_health_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STUB_LLM_A_URL", "http://stub-a:8000")
    registry = [dict(entry) for entry in _models(_REGISTRY_TWO_LOCAL)]
    registry[0]["health_path"] = "/readyz"

    resolved, skipped = handler_module._resolve_models(registry, ["stub-local-a"])

    assert skipped == []
    assert resolved[0].health_path == "/readyz"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_probe_health_uses_registry_health_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_urls: list[str] = []

    class FakeAsyncClient:
        def __init__(self, timeout: float) -> None:
            assert timeout == handler_module._HEALTH_PROBE_TIMEOUT

        async def __aenter__(self) -> FakeAsyncClient:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def get(self, url: str) -> SimpleNamespace:
            seen_urls.append(url)
            return SimpleNamespace(status_code=200)

    monkeypatch.setattr(handler_module.httpx, "AsyncClient", FakeAsyncClient)
    model = handler_module._ResolvedModel(
        model_id="local-model",
        display_name="Local Model",
        endpoint_url="http://runtime.local/",
        endpoint_path="/v1/chat/completions",
        health_path="/readyz",
        protocol="openai_compatible",
        model_id_resolved="local-model-default",
        cost_per_1k_input=0.0,
        cost_per_1k_output=0.0,
        location="local",
        context_window=8192,
    )

    assert await handler_module._probe_health(model) is True
    assert seen_urls == ["http://runtime.local/readyz"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_openai_compat_empty_choices_returns_empty_output() -> None:
    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "usage": {"prompt_tokens": 7, "completion_tokens": 11},
                "choices": [],
            }

    class FakeAsyncClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> FakeAsyncClient:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(
            self,
            _endpoint_url: str,
            *,
            json: dict[str, object],
            headers: dict[str, str],
        ) -> FakeResponse:
            assert json["model"] == "model-a"
            assert headers["Content-Type"] == "application/json"
            return FakeResponse()

    monkeypatch_target = handler_module.httpx
    with patch.object(monkeypatch_target, "AsyncClient", FakeAsyncClient):
        (
            raw_output,
            prompt_tokens,
            completion_tokens,
            _latency_ms,
        ) = await handler_module._DefaultHttpLlmClient()._call_openai_compat(
            endpoint_url="http://local:8000/v1/chat/completions",
            model_id="model-a",
            system_prompt="system",
            user_prompt="user",
            timeout_seconds=120.0,
            api_key=None,
            start=0.0,
        )

    assert raw_output == ""
    assert prompt_tokens == 7
    assert completion_tokens == 11


@pytest.mark.unit
def test_run_quality_check_removes_temp_file_on_subprocess_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created_paths: list[Path] = []
    real_named_temporary_file = tempfile.NamedTemporaryFile

    def fake_named_temporary_file(*args: object, **kwargs: object) -> object:
        handle = real_named_temporary_file(*args, **kwargs)
        created_paths.append(Path(handle.name))
        return handle

    def raise_timeout(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess:
        raise subprocess.TimeoutExpired(cmd=["ruff"], timeout=10)

    monkeypatch.setattr(tempfile, "NamedTemporaryFile", fake_named_temporary_file)
    monkeypatch.setattr(handler_module.subprocess, "run", raise_timeout)

    result = handler_module._run_quality_check("print('hello')\n")

    assert result.startswith("skip:")
    assert len(created_paths) == 1
    assert not created_paths[0].exists()


@pytest.mark.unit
def test_calculate_cost_local_is_zero() -> None:
    model = handler_module._ResolvedModel(
        model_id="local-a",
        display_name="Local A",
        endpoint_url="http://local:8000",
        endpoint_path="/v1/chat/completions",
        protocol="openai_compatible",
        model_id_resolved="model-a",
        cost_per_1k_input=0.0,
        cost_per_1k_output=0.0,
        location="local",
        context_window=8192,
    )

    assert (
        handler_module._calculate_cost(model, prompt_tokens=1000, completion_tokens=500)
        == 0.0
    )


@pytest.mark.unit
def test_calculate_cost_cloud_correctness() -> None:
    model = handler_module._ResolvedModel(
        model_id="cloud-b",
        display_name="Cloud B",
        endpoint_url="http://cloud:9000",
        endpoint_path="/v1/chat/completions",
        protocol="openai_compatible",
        model_id_resolved="model-b",
        cost_per_1k_input=0.003,
        cost_per_1k_output=0.015,
        location="cloud",
        context_window=200000,
    )

    cost = handler_module._calculate_cost(
        model, prompt_tokens=500, completion_tokens=200
    )

    assert abs(cost - 0.0045) < 1e-9


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_fans_out_to_all_available_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STUB_LLM_A_URL", "http://stub-a:8000")
    monkeypatch.setenv("STUB_LLM_B_URL", "http://stub-b:9000")
    monkeypatch.setenv("STUB_LLM_B_API_KEY", "test-key")

    fake = FakeLlmClient()
    reg_path = _write_registry(_REGISTRY_TWO_LOCAL)

    with patch.object(handler_module, "_probe_health", AsyncMock(return_value=True)):
        handler = _make_handler_with_registry(reg_path, fake)
        result = await handler.handle(
            ModelAbCompareStart(
                task="Write a UUID validator", correlation_id="test-corr-1"
            )
        )

    assert len(result.comparison) == 2
    model_keys = {r.model_key for r in result.comparison}
    assert "stub-local-a" in model_keys
    assert "stub-cloud-b" in model_keys
    Path(reg_path).unlink(missing_ok=True)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_local_model_cost_is_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STUB_LLM_A_URL", "http://stub-a:8000")
    monkeypatch.delenv("STUB_LLM_B_API_KEY", raising=False)

    fake = FakeLlmClient()
    reg_path = _write_registry(_REGISTRY_TWO_LOCAL)

    with patch.object(handler_module, "_probe_health", AsyncMock(return_value=True)):
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
    monkeypatch.setenv("STUB_LLM_B_URL", "http://stub-b:9000")
    monkeypatch.setenv("STUB_LLM_B_API_KEY", "test-key")
    monkeypatch.delenv("STUB_LLM_A_URL", raising=False)

    fake = FakeLlmClient()
    reg_path = _write_registry(_REGISTRY_TWO_LOCAL)

    with patch.object(handler_module, "_probe_health", AsyncMock(return_value=True)):
        handler = _make_handler_with_registry(reg_path, fake)
        result = await handler.handle(
            ModelAbCompareStart(task="test", correlation_id="corr-cloud-cost")
        )

    cloud_rows = [r for r in result.comparison if r.model_key == "stub-cloud-b"]
    assert len(cloud_rows) == 1
    expected = (50 * 0.003 + 100 * 0.015) / 1000.0
    assert abs(cloud_rows[0].cost_usd - expected) < 1e-9
    Path(reg_path).unlink(missing_ok=True)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_rows_sorted_by_cost_ascending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STUB_LLM_A_URL", "http://stub-a:8000")
    monkeypatch.setenv("STUB_LLM_B_URL", "http://stub-b:9000")
    monkeypatch.setenv("STUB_LLM_B_API_KEY", "test-key")

    fake = FakeLlmClient()
    reg_path = _write_registry(_REGISTRY_TWO_LOCAL)

    with patch.object(handler_module, "_probe_health", AsyncMock(return_value=True)):
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
    monkeypatch.delenv("STUB_CLOUD_URL", raising=False)

    fake = FakeLlmClient()
    reg_path = _write_registry(_REGISTRY_WITH_MISSING_KEY)

    with patch.object(handler_module, "_probe_health", AsyncMock(return_value=True)):
        handler = _make_handler_with_registry(reg_path, fake)
        result = await handler.handle(
            ModelAbCompareStart(task="test", correlation_id="corr-skip-key")
        )

    assert "stub-cloud-no-key" in result.models_skipped
    assert len(result.comparison) == 0
    Path(reg_path).unlink(missing_ok=True)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_skips_unreachable_health_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STUB_LLM_A_URL", "http://stub-a:8000")
    monkeypatch.delenv("STUB_LLM_B_API_KEY", raising=False)

    fake = FakeLlmClient()
    reg_path = _write_registry(_REGISTRY_TWO_LOCAL)

    with patch.object(handler_module, "_probe_health", AsyncMock(return_value=False)):
        handler = _make_handler_with_registry(reg_path, fake)
        result = await handler.handle(
            ModelAbCompareStart(task="test", correlation_id="corr-health")
        )

    assert len(result.comparison) == 0
    assert "stub-local-a" in result.models_skipped
    Path(reg_path).unlink(missing_ok=True)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handle_specific_model_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STUB_LLM_A_URL", "http://stub-a:8000")
    monkeypatch.setenv("STUB_LLM_B_URL", "http://stub-b:9000")
    monkeypatch.setenv("STUB_LLM_B_API_KEY", "test-key")

    fake = FakeLlmClient()
    reg_path = _write_registry(_REGISTRY_TWO_LOCAL)

    with patch.object(handler_module, "_probe_health", AsyncMock(return_value=True)):
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
    monkeypatch.setenv("STUB_LLM_A_URL", "http://stub-a:8000")
    monkeypatch.delenv("STUB_LLM_B_API_KEY", raising=False)

    fake = FakeLlmClient()
    reg_path = _write_registry(_REGISTRY_TWO_LOCAL)

    with patch.object(handler_module, "_probe_health", AsyncMock(return_value=True)):
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
    monkeypatch.setenv("STUB_LLM_A_URL", "http://stub-a:8000")
    monkeypatch.setenv("STUB_LLM_B_URL", "http://stub-b:9000")
    monkeypatch.setenv("STUB_LLM_B_API_KEY", "test-key")

    fake = FakeLlmClient()
    reg_path = _write_registry(_REGISTRY_TWO_LOCAL)

    with patch.object(handler_module, "_probe_health", AsyncMock(return_value=True)):
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
    monkeypatch.setenv("STUB_LLM_A_URL", "http://stub-a:8000")
    monkeypatch.delenv("STUB_LLM_B_API_KEY", raising=False)

    fake = FakeLlmClient(responses={"stub-model-a": (_FAKE_OUTPUT, 42, 88, 300)})
    reg_path = _write_registry(_REGISTRY_TWO_LOCAL)

    with patch.object(handler_module, "_probe_health", AsyncMock(return_value=True)):
        handler = _make_handler_with_registry(reg_path, fake)
        result = await handler.handle(
            ModelAbCompareStart(task="test", correlation_id="corr-tokens")
        )

    row = result.comparison[0]
    assert row.prompt_tokens == 42
    assert row.completion_tokens == 88
    assert row.total_tokens == 130
    Path(reg_path).unlink(missing_ok=True)
