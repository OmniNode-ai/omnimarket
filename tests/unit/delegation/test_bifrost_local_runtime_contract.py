# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Bifrost local render hints and overlay-owned served ids."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.adapters.llm.bifrost.config_loader_bifrost_delegation import (
    load_bifrost_delegation_config,
)
from omnimarket.models.delegation.wire.model_bifrost_delegation_config import (
    ModelDelegationBackendConfig,
)
from omnimarket.routing.delegation_backend_resolution import resolve_delegation_backend

from omnimarket.adapters.llm.bifrost.config_loader_bifrost_delegation import (
    load_bifrost_delegation_config,
)

_CONTRACT_PATH = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/configs/bifrost_delegation.yaml"
)


@pytest.mark.unit
def test_only_local_qwen_backends_declare_inline_reasoning(tmp_path: Path) -> None:
    overlay = tmp_path / "empty.yaml"
    overlay.write_text("backends: []\n", encoding="utf-8")
    config = load_bifrost_delegation_config(_CONTRACT_PATH, overlay)
    for backend in config.backends:
        expected = (
            "</think>"
            if backend.backend_id in {"local-coder", "local-heavy-reasoning"}
            else None
        )
        assert backend.inline_reasoning_terminator == expected, backend.backend_id


@pytest.mark.unit
@pytest.mark.parametrize("terminator", ["</think>", None])
def test_resolved_backend_carries_inline_reasoning_contract(
    terminator: str | None,
) -> None:
    resolved = resolve_delegation_backend(
        "code_generation",
        backends=[
            {
                "backend_id": "local-coder",
                "model_name": "test-qwen",
                "endpoint_url": "https://inference.example/v1/chat/completions",
                "tier": "local",
                "capabilities": ["code_generation"],
                "max_tokens": 4096,
                "timeout_ms": 60000,
                "inline_reasoning_terminator": terminator,
            }
        ],
    )
    assert resolved.inline_reasoning_terminator == terminator


@pytest.mark.unit
def test_reasoning_parser_lane_overlay_clears_inline_reasoning(tmp_path: Path) -> None:
    overlay = tmp_path / "parser.yaml"
    overlay.write_text(
        "backends:\n"
        "  - backend_id: local-coder\n"
        "    inline_reasoning_terminator: null\n"
        "  - backend_id: local-heavy-reasoning\n"
        "    inline_reasoning_terminator: null\n",
        encoding="utf-8",
    )
    config = load_bifrost_delegation_config(_CONTRACT_PATH, overlay)
    assert all(
        backend.inline_reasoning_terminator is None for backend in config.backends
    )


@pytest.mark.unit
def test_backend_undeclared_terminator_defaults_to_none_and_empty_is_invalid() -> None:
    fields = {"backend_id": "test", "model_name": "test", "tier": "local"}
    assert (
        ModelDelegationBackendConfig.model_validate(fields).inline_reasoning_terminator
        is None
    )
    with pytest.raises(ValidationError, match="inline_reasoning_terminator"):
        ModelDelegationBackendConfig.model_validate(
            {**fields, "inline_reasoning_terminator": ""}
        )


@pytest.mark.unit
def test_local_delegation_backends_declare_renderable_endpoint_envs(
    tmp_path: Path,
) -> None:
    contract = yaml.safe_load(_CONTRACT_PATH.read_text(encoding="utf-8"))
    backends = {backend["backend_id"]: backend for backend in contract["backends"]}
    expected_envs = {
        "local-coder": "BIFROST_LOCAL_CODER_ENDPOINT_URL",
        "local-heavy-reasoning": "BIFROST_LOCAL_CODER_ENDPOINT_URL",
        "local-ds-v4-flash": "BIFROST_LOCAL_DS_V4_FLASH_ENDPOINT_URL",
    }
    for backend_id, env in expected_envs.items():
        assert backends[backend_id]["endpoint_url_env"] == env
    for retired in ("local-reasoner", "local-coder-mlx"):
        assert retired not in backends
    served_ids = {
        "local-coder": "Qwen3.8-27B",
        "local-heavy-reasoning": "Qwen3.8-27B",
        "local-embedding": "text-embedding-qwen3",
        "local-ds-v4-flash": "deepseek-v4-flash",
    }
    assert all(backends[backend_id]["model_name"] is None for backend_id in served_ids)
    overlay = tmp_path / "lab-overlay.yaml"
    overlay.write_text(
        yaml.safe_dump(
            {
                "backends": [
                    {
                        "backend_id": backend_id,
                        "model_name": model,
                        "endpoint_url": "http://lab-fixture.invalid:8000/v1/chat/completions",
                    }
                    for backend_id, model in served_ids.items()
                ]
            }
        ),
        encoding="utf-8",
    )
    loaded = load_bifrost_delegation_config(
        config_path=_CONTRACT_PATH, overlay_path=overlay
    )
    assert {
        backend.backend_id: backend.model_name
        for backend in loaded.backends
        if backend.tier == "local"
    } == served_ids
