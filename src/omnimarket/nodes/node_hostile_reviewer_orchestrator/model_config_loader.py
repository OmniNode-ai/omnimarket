# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-driven model config loader for node_hostile_reviewer_orchestrator.

Reads the ``model_routing`` policy schema from this node's ``contract.yaml`` and
requires callers to provide logical route keys plus matching runtime route
configs. Missing route inputs fail loudly; this loader does not substitute
fallback model IDs.

Re-homed from the deleted node_hostile_reviewer shell (OMN-13210 / B1) so the
caller-supplied route policy survives the rebuild.

Related: OMN-7981, OMN-11936, OMN-13210
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from omnimarket.inference.adapter_inference_bridge import (
    AdapterInferenceBridge,
    ModelInferenceBridgeConfig,
)
from omnimarket.inference.bridge_config_loader import resolve_bifrost_backend

_CONTRACT_PATH = Path(__file__).parent / "contract.yaml"
_DEFAULT_ROUTE_CONFIG_ENV = "HOSTILE_REVIEWER_MODEL_CONFIGS_JSON"
_LAB_BACKEND_PROVIDER = "local"
_CHAT_COMPLETIONS_SUFFIX = "/chat/completions"


def _load_contract_model_routing() -> dict[str, Any]:
    data = yaml.safe_load(_CONTRACT_PATH.read_text())
    return data.get("model_routing", {})  # type: ignore[no-any-return]


def _route_config_env_name(routing_policy: Mapping[str, Any]) -> str:
    raw_env_name = routing_policy.get("route_config_env", _DEFAULT_ROUTE_CONFIG_ENV)
    env_name = str(raw_env_name).strip()
    if not env_name:
        msg = "hostile reviewer model_routing.route_config_env is required"
        raise ValueError(msg)
    return env_name


def _load_runtime_model_configs(
    routing_policy: Mapping[str, Any],
) -> dict[str, Mapping[str, Any]]:
    env_name = _route_config_env_name(routing_policy)
    raw_configs = os.environ.get(env_name, "")
    if not raw_configs:
        msg = (
            "hostile reviewer runtime model configs are required; "
            f"set {env_name} to a JSON object keyed by logical route key"
        )
        raise ValueError(msg)

    try:
        decoded = json.loads(raw_configs)
    except json.JSONDecodeError as exc:
        msg = f"{env_name} must contain valid JSON"
        raise ValueError(msg) from exc

    if not isinstance(decoded, Mapping):
        msg = f"{env_name} must contain a JSON object keyed by logical route key"
        raise ValueError(msg)

    configs: dict[str, Mapping[str, Any]] = {}
    for key, declaration in decoded.items():
        if not isinstance(key, str) or not key.strip():
            msg = f"{env_name} contains an invalid logical route key: {key!r}"
            raise ValueError(msg)
        if not isinstance(declaration, Mapping):
            msg = f"{env_name}.{key} must be an object"
            raise ValueError(msg)
        configs[key] = declaration
    return configs


def _declared_lab_route_configs(
    requested_keys: list[str],
) -> dict[str, Mapping[str, Any]] | None:
    """Resolve every requested key against the lab backends the repo declares.

    A key resolves only when the bifrost delegation contract (plus the lane
    overlay) declares it as a ``local`` backend with a complete chat-completions
    endpoint and a served model name. Returns ``None`` when any key does not, so
    the caller fails closed instead of mixing declared and invented routes.
    """
    declarations: dict[str, Mapping[str, Any]] = {}
    for key in requested_keys:
        backend = resolve_bifrost_backend(key)
        if backend is None or backend.provider != _LAB_BACKEND_PROVIDER:
            return None
        endpoint = (backend.endpoint_url or "").strip()
        model_id = (backend.model_name or "").strip()
        if not endpoint.endswith(_CHAT_COMPLETIONS_SUFFIX) or not model_id:
            return None
        declarations[key] = {
            "transport": "http",
            # The inference adapter appends the chat-completions path itself.
            "base_url": endpoint[: -len(_CHAT_COMPLETIONS_SUFFIX)],
            "model_id": model_id,
        }
    return declarations


def _require_requested_keys(requested_keys: list[str] | None) -> list[str]:
    if not requested_keys:
        msg = "hostile reviewer requires caller-provided logical model route keys"
        raise ValueError(msg)
    return requested_keys


def _coerce_http_config(
    key: str,
    declaration: Mapping[str, Any],
) -> dict[str, object]:
    raw_base_url = str(declaration.get("base_url", "")).strip()
    base_url_env = str(declaration.get("base_url_env", "")).strip()
    if raw_base_url:
        base_url = raw_base_url
    elif base_url_env:
        base_url = os.environ.get(base_url_env, "").strip()
        if not base_url:
            msg = (
                f"hostile reviewer route {key!r} requires env var "
                f"{base_url_env!r} to resolve base_url"
            )
            raise ValueError(msg)
    else:
        msg = f"hostile reviewer route {key!r} requires base_url or base_url_env"
        raise ValueError(msg)

    model_id = str(declaration.get("model_id", "")).strip()
    if not model_id:
        msg = f"hostile reviewer route {key!r} requires runtime model_id"
        raise ValueError(msg)

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


# Route config is data (contract default or route-config env), and its cli_args
# are splatted into a subprocess argv. A command may therefore only receive the
# argument tokens listed here, so an override cannot widen the Codex sandbox
# (OMN-17427). A command with no entry accepts no cli_args at all.
_ALLOWED_CLI_ARGS: dict[str, frozenset[str]] = {
    "codex": frozenset({"exec", "-s", "read-only", "--skip-git-repo-check"}),
}


def _validated_cli_args(key: str, cli_command: str, cli_args: object) -> list[str]:
    if not isinstance(cli_args, list) or not all(
        isinstance(arg, str) for arg in cli_args
    ):
        msg = f"hostile reviewer route {key!r} cli_args must be a list of strings"
        raise ValueError(msg)
    allowed = _ALLOWED_CLI_ARGS.get(Path(cli_command).name, frozenset())
    refused = [arg for arg in cli_args if arg not in allowed]
    if refused:
        msg = (
            f"hostile reviewer route {key!r} cli_args {refused!r} are not in the "
            f"allowlist for {cli_command!r}"
        )
        raise ValueError(msg)
    return list(cli_args)


def _coerce_cli_config(
    key: str,
    declaration: Mapping[str, Any],
) -> dict[str, object]:
    cli_command = str(declaration.get("cli_command", "")).strip()
    if not cli_command:
        msg = f"hostile reviewer route {key!r} requires cli_command"
        raise ValueError(msg)

    config: dict[str, object] = {
        "transport": "cli",
        "cli_command": cli_command,
        "context_window": int(declaration.get("context_window", 64000)),
        "timeout_seconds": float(declaration.get("timeout_seconds", 120.0)),
    }
    cli_args = declaration.get("cli_args")
    if cli_args is not None:
        config["cli_args"] = _validated_cli_args(key, cli_command, cli_args)
    if "fail_on_nonzero_exit" in declaration:
        config["fail_on_nonzero_exit"] = bool(declaration["fail_on_nonzero_exit"])
    return config


def _coerce_model_config(
    key: str,
    declaration: Mapping[str, Any],
) -> dict[str, object]:
    transport = str(declaration.get("transport", "")).strip()
    if not transport:
        msg = f"hostile reviewer route {key!r} requires transport"
        raise ValueError(msg)

    if transport == "http":
        return _coerce_http_config(key, declaration)
    if transport == "cli":
        return _coerce_cli_config(key, declaration)

    msg = f"hostile reviewer route {key!r} has unsupported transport: {transport!r}"
    raise ValueError(msg)


def build_model_configs(
    requested_keys: list[str] | None = None,
    runtime_model_configs: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, dict[str, object]]:
    """Build per-model config dicts from caller-provided logical route keys.

    The contract declares the policy schema, not concrete model defaults.
    Runtime configs may be supplied directly for tests/callers, or via the
    contract's route_config_env JSON object. When that env is unset, keys the
    bifrost delegation contract declares as lab (``local``) backends resolve
    from it.

    Args:
        requested_keys: Required logical route keys from the start command.
        runtime_model_configs: Optional runtime configs keyed by logical route key.

    Returns:
        Dict mapping logical route key -> per-model adapter config.
    """
    keys = _require_requested_keys(requested_keys)
    routing_policy = _load_contract_model_routing()
    if runtime_model_configs is not None:
        route_configs = dict(runtime_model_configs)
    elif os.environ.get(_route_config_env_name(routing_policy), ""):
        route_configs = _load_runtime_model_configs(routing_policy)
    else:
        # No route-config JSON on this runtime: the lab backends the repo
        # already declares are the only other source. Anything they cannot
        # answer for raises the route-config-required error below.
        declared = _declared_lab_route_configs(keys)
        route_configs = (
            declared
            if declared is not None
            else _load_runtime_model_configs(routing_policy)
        )
    configs: dict[str, dict[str, object]] = {}

    for key in keys:
        declaration = route_configs.get(key)
        if declaration is None:
            msg = f"hostile reviewer route {key!r} is not configured"
            raise ValueError(msg)
        configs[key] = _coerce_model_config(key, declaration)

    return configs


@dataclass(frozen=True, slots=True)
class ModelFallbackRoute:
    """The contract-declared review-engine fallback, resolved to a route config."""

    key: str
    config: dict[str, object]
    timeout_seconds: float


def load_fallback_route(
    runtime_model_configs: Mapping[str, Mapping[str, Any]] | None = None,
) -> ModelFallbackRoute | None:
    """Resolve ``model_routing.fallback`` (OMN-17427), or ``None`` if undeclared.

    A route config under the fallback ``route_key`` in the route-config env (or
    in ``runtime_model_configs``) overrides the contract's ``default_route``.
    Unlike requested keys the fallback is optional in the env: an unset env
    leaves the contract default in force.
    """
    fallback = _load_contract_model_routing().get("fallback")
    if not fallback:
        return None
    key = str(fallback.get("route_key", "")).strip()
    if not key:
        msg = "hostile reviewer model_routing.fallback.route_key is required"
        raise ValueError(msg)
    if runtime_model_configs is not None:
        runtime_configs: Mapping[str, Mapping[str, Any]] = runtime_model_configs
    elif os.environ.get(_route_config_env_name(_load_contract_model_routing())):
        runtime_configs = _load_runtime_model_configs(_load_contract_model_routing())
    else:
        runtime_configs = {}
    declaration = runtime_configs.get(key) or fallback.get("default_route")
    if not isinstance(declaration, Mapping):
        msg = f"hostile reviewer fallback route {key!r} has no route config"
        raise ValueError(msg)
    config = _coerce_model_config(key, declaration)
    return ModelFallbackRoute(
        key=key, config=config, timeout_seconds=float(str(config["timeout_seconds"]))
    )


def build_from_contract(
    requested_keys: list[str] | None = None,
    runtime_model_configs: Mapping[str, Mapping[str, Any]] | None = None,
) -> AdapterInferenceBridge:
    """Build an AdapterInferenceBridge from logical route keys + runtime configs.

    The node contract owns the policy schema. Concrete route configs must be
    supplied by the caller or via the contract-declared JSON env var. Missing
    requested keys or incomplete route configs raise ValueError.
    """
    configs = build_model_configs(
        requested_keys=requested_keys,
        runtime_model_configs=runtime_model_configs,
    )
    return AdapterInferenceBridge(ModelInferenceBridgeConfig(model_configs=configs))


__all__: list[str] = [
    "ModelFallbackRoute",
    "build_from_contract",
    "build_model_configs",
    "load_fallback_route",
]
