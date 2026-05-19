# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Bifrost default + overlay loading tests for OMN-10717."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml

from omnimarket.adapters.llm.bifrost.config_loader_bifrost_delegation import (
    deep_merge_bifrost_delegation_config,
    load_bifrost_delegation_config,
)

_DEFAULT_CONTRACT = textwrap.dedent(
    """\
    config_version: "1.2.0"
    schema_version: "bifrost_delegation.v1"
    backends:
      - backend_id: local-qwen-coder-30b
        endpoint_url: ""
        model_name: qwen-coder
        tier: local
        timeout_ms: 30000
        capabilities: [code_generation]
      - backend_id: future-backend
        endpoint_url: ""
        model_name: future-model
        tier: local
        timeout_ms: 30000
        capabilities: [research]
    routing_rules:
      - rule_id: "d4e5f6a7-0001-4000-8000-000000000001"
        priority: 10
        task_class: code_generation
        task_class_contract_version: "1.0.0"
        backend_policy_version: "1.0.0"
        match_operation_types: [chat_completion]
        match_capabilities: [code_generation]
        backend_ids: [local-qwen-coder-30b, future-backend]
        fallback_policy:
          action: escalate_to_next_tier
          max_retries: 1
          on_exhaust: return_error
        shadow_policy_id: "e5f6a7b8-0001-4000-8000-000000000001"
    default_backends:
      - local-qwen-coder-30b
      - future-backend
    circuit_breaker:
      failure_threshold: 5
      window_seconds: 30
    failover:
      max_attempts: 3
      backoff_base_ms: 500
    shadow_mode:
      enabled: false
      policy_version: "unknown"
      log_sample_rate: 1.0
      comparison_logging_enabled: true
      max_shadow_latency_ms: 5.0
    """
)


@pytest.mark.unit
def test_canonical_bifrost_contract_has_empty_endpoint_urls() -> None:
    path = Path("src/omnimarket/configs/bifrost_delegation.yaml")
    data = yaml.safe_load(path.read_text())

    endpoints = [backend.get("endpoint_url") for backend in data["backends"]]

    assert endpoints
    assert all(endpoint == "" for endpoint in endpoints)


@pytest.mark.unit
def test_deep_merge_preserves_new_default_backend_with_overlay_endpoint() -> None:
    default = yaml.safe_load(_DEFAULT_CONTRACT)
    overlay = {
        "backends": [
            {
                "backend_id": "local-qwen-coder-30b",
                "endpoint_url": "https://local.test:8000",
            }
        ]
    }

    merged = deep_merge_bifrost_delegation_config(default, overlay)

    by_id = {backend["backend_id"]: backend for backend in merged["backends"]}
    assert by_id["local-qwen-coder-30b"]["endpoint_url"] == "https://local.test:8000"
    assert by_id["local-qwen-coder-30b"]["model_name"] == "qwen-coder"
    assert by_id["future-backend"]["endpoint_url"] == ""


@pytest.mark.unit
def test_loader_deep_merges_overlay_file(tmp_path: Path) -> None:
    default_path = tmp_path / "bifrost_delegation.yaml"
    overlay_path = tmp_path / "bifrost_overrides.yaml"
    default_path.write_text(_DEFAULT_CONTRACT)
    overlay_path.write_text(
        textwrap.dedent(
            """\
            backends:
              - backend_id: local-qwen-coder-30b
                endpoint_url: "https://local.test:8000"
            """
        )
    )

    config = load_bifrost_delegation_config(default_path, overlay_path)

    by_id = {backend.backend_id: backend for backend in config.backends}
    assert by_id["local-qwen-coder-30b"].endpoint_url == "https://local.test:8000"
    assert by_id["future-backend"].model_name == "future-model"


@pytest.mark.unit
def test_routing_loader_uses_overlay_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
        handler_delegation_routing as routing,
    )

    default_path = tmp_path / "bifrost_delegation.yaml"
    overlay_path = tmp_path / "bifrost_overrides.yaml"
    default_path.write_text(_DEFAULT_CONTRACT)
    overlay_path.write_text(
        "backends:\n"
        "  - backend_id: local-qwen-coder-30b\n"
        '    endpoint_url: "https://local.test:8000"\n'
    )
    monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(default_path))
    monkeypatch.setenv("BIFROST_OVERLAY_PATH", str(overlay_path))
    routing._load_bifrost_endpoints.cache_clear()

    endpoints = routing._load_bifrost_endpoints()

    assert endpoints["local-qwen-coder-30b"].endpoint_url == "https://local.test:8000"
    assert "future-backend" not in endpoints
