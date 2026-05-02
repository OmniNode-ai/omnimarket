# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from omnimarket.nodes.node_ab_compare_orchestrator.handlers import (
    handler_ab_compare_orchestrator as handler_module,
)


def _local_registry_entry(health_path: str = "/readyz") -> dict[str, object]:
    return {
        "id": "local-model",
        "display_name": "Local Model",
        "endpoint_env": "LOCAL_MODEL_URL",
        "endpoint_path": "/v1/chat/completions",
        "health_path": health_path,
        "protocol": "openai_compatible",
        "model_id_default": "local-model-default",
        "cost_per_1k_input": 0.0,
        "cost_per_1k_output": 0.0,
        "location": "local",
        "context_window": 8192,
    }


@pytest.mark.unit
def test_resolve_models_preserves_health_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOCAL_MODEL_URL", "http://runtime.local/")

    resolved, skipped = handler_module._resolve_models(
        [_local_registry_entry("/livez")],
        ["local-model"],
    )

    assert skipped == []
    assert len(resolved) == 1
    assert resolved[0].endpoint_url == "http://runtime.local"
    assert resolved[0].health_path == "/livez"


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
