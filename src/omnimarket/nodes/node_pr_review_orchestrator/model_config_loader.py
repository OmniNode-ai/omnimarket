# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-driven model config loader for node_pr_review_orchestrator.

Reads the ``model_routing`` policy schema from this node's ``contract.yaml`` and
requires callers to provide reviewer + judge logical route keys plus matching
runtime route configs. Missing route inputs fail loudly; this loader does not
substitute fallback model IDs.

Re-expressed from the deleted node_pr_review_bot workflow_runner endpoint
resolution (OMN-13212 / B2) so the caller-supplied route policy survives the
rebuild. Reuses the same route-config env declared on the contract.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from omnimarket.inference.adapter_inference_bridge import ModelInferenceBridgeConfig

_CONTRACT_PATH = Path(__file__).parent / "contract.yaml"
_DEFAULT_ROUTE_CONFIG_ENV = "PR_REVIEW_MODEL_CONFIGS_JSON"


def _load_contract_model_routing() -> dict[str, Any]:
    data = yaml.safe_load(_CONTRACT_PATH.read_text())
    return data.get("model_routing", {})  # type: ignore[no-any-return]


def _route_config_env_name(routing_policy: Mapping[str, Any]) -> str:
    raw_env_name = routing_policy.get("route_config_env", _DEFAULT_ROUTE_CONFIG_ENV)
    env_name = str(raw_env_name).strip()
    if not env_name:
        raise ValueError("pr review model_routing.route_config_env is required")
    return env_name


def _load_runtime_model_configs(
    routing_policy: Mapping[str, Any],
) -> dict[str, Mapping[str, Any]]:
    env_name = _route_config_env_name(routing_policy)
    raw_configs = os.environ.get(env_name, "")
    if not raw_configs:
        raise ValueError(
            "pr review runtime model configs are required; "
            f"set {env_name} to a JSON object keyed by logical route key"
        )
    try:
        decoded = json.loads(raw_configs)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{env_name} must contain valid JSON") from exc
    if not isinstance(decoded, Mapping):
        raise ValueError(
            f"{env_name} must contain a JSON object keyed by logical route key"
        )
    configs: dict[str, Mapping[str, Any]] = {}
    for key, declaration in decoded.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError(
                f"{env_name} contains an invalid logical route key: {key!r}"
            )
        if not isinstance(declaration, Mapping):
            raise ValueError(f"{env_name}.{key} must be an object")
        configs[key] = declaration
    return configs


def _coerce_http_config(key: str, declaration: Mapping[str, Any]) -> dict[str, object]:
    raw_base_url = str(declaration.get("base_url", "")).strip()
    base_url_env = str(declaration.get("base_url_env", "")).strip()
    if raw_base_url:
        base_url = raw_base_url
    elif base_url_env:
        base_url = os.environ.get(base_url_env, "").strip()
        if not base_url:
            raise ValueError(
                f"pr review route {key!r} requires env var "
                f"{base_url_env!r} to resolve base_url"
            )
    else:
        raise ValueError(f"pr review route {key!r} requires base_url or base_url_env")

    model_id = str(declaration.get("model_id", "")).strip()
    if not model_id:
        raise ValueError(f"pr review route {key!r} requires runtime model_id")

    config: dict[str, object] = {
        "transport": "http",
        "base_url": base_url.rstrip("/"),
        "model_id": model_id,
        "context_window": int(declaration.get("context_window", 32000)),
        "timeout_seconds": float(declaration.get("timeout_seconds", 90.0)),
    }
    for optional_key in ("api_key", "temperature", "extra_headers"):
        if optional_key in declaration:
            config[optional_key] = declaration[optional_key]
    return config


def build_model_configs(
    requested_keys: list[str],
    runtime_model_configs: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, dict[str, object]]:
    """Build per-model HTTP config dicts from caller-provided logical route keys.

    The contract declares the policy schema, not concrete model defaults. Runtime
    configs may be supplied directly (tests/callers) or via the contract's
    route_config_env JSON object. PR review requires http inference endpoints.
    """
    if not requested_keys:
        raise ValueError("pr review requires caller-provided logical model route keys")
    routing_policy = _load_contract_model_routing()
    route_configs = (
        dict(runtime_model_configs)
        if runtime_model_configs is not None
        else _load_runtime_model_configs(routing_policy)
    )
    configs: dict[str, dict[str, object]] = {}
    for key in requested_keys:
        declaration = route_configs.get(key)
        if declaration is None:
            raise ValueError(f"pr review route {key!r} is not configured")
        transport = str(declaration.get("transport", "http")).strip() or "http"
        if transport != "http":
            raise ValueError(
                f"pr review route {key!r} uses unsupported transport {transport!r}; "
                "PR review requires http inference endpoints."
            )
        configs[key] = _coerce_http_config(key, declaration)
    return configs


def build_bridge_config(
    reviewer_models: list[str],
    judge_model: str,
    runtime_model_configs: Mapping[str, Mapping[str, Any]] | None = None,
) -> ModelInferenceBridgeConfig:
    """Build an inference bridge config covering reviewer + judge route keys."""
    requested = [*reviewer_models, judge_model]
    configs = build_model_configs(requested, runtime_model_configs)
    return ModelInferenceBridgeConfig(model_configs=configs)


__all__: list[str] = ["build_bridge_config", "build_model_configs"]
