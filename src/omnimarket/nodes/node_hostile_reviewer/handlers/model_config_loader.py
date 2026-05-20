# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-driven model config loader for node_hostile_reviewer.

Reads model_routing declarations from contract.yaml and resolves endpoint URLs
from environment variables (no hardcoded IPs). Skips unavailable models with
a warning so the reviewer runs with N-1 models instead of failing outright.

Related: OMN-7981
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_CONTRACT_PATH = Path(__file__).parents[1] / "contract.yaml"


def _load_contract_model_routing() -> dict[str, Any]:
    data = yaml.safe_load(_CONTRACT_PATH.read_text())
    return data.get("model_routing", {})  # type: ignore[no-any-return]


def build_model_configs(
    requested_keys: list[str] | None = None,
) -> dict[str, dict[str, object]]:
    """Build per-model config dicts from contract.yaml model_routing.

    Each entry in model_routing declares endpoint_env (the env var name holding
    the base URL), transport, context_window, and optional cli_command. This
    function resolves the env vars at runtime and returns a dict suitable for
    ModelInferenceBridgeConfig.model_configs.

    Models whose endpoint_env is unset (or empty string for CLI models) are
    included only when the env var is set or transport is "cli". CLI models are
    always included if their cli_command is non-empty.

    Args:
        requested_keys: Subset of model_routing keys to load. None means all.

    Returns:
        Dict mapping model_key -> per-model config dict. Empty for unavailable models.
    """
    routing = _load_contract_model_routing()
    configs: dict[str, dict[str, object]] = {}
    unavailable: list[str] = []

    for key, declaration in routing.items():
        if requested_keys is not None and key not in requested_keys:
            continue

        transport = str(declaration.get("transport", "http"))
        endpoint_env = str(declaration.get("endpoint_env", ""))
        cli_command = str(declaration.get("cli_command", ""))

        if transport == "cli":
            if not cli_command:
                logger.warning(
                    "[hostile_reviewer] model %r: transport=cli but cli_command empty, skipping",
                    key,
                )
                unavailable.append(key)
                continue
            configs[key] = {
                "transport": "cli",
                "cli_command": cli_command,
                "context_window": int(declaration.get("context_window", 64000)),
                "timeout_seconds": float(declaration.get("timeout_seconds", 120.0)),
            }
        else:
            base_url = os.environ.get(endpoint_env, "") if endpoint_env else ""
            if not base_url:
                logger.warning(
                    "[hostile_reviewer] model %r: env var %r not set, skipping (N-1 graceful degradation)",
                    key,
                    endpoint_env,
                )
                unavailable.append(key)
                continue
            configs[key] = {
                "transport": "http",
                "base_url": base_url.rstrip("/"),
                "model_id": str(declaration.get("model_id", "default")),
                "context_window": int(declaration.get("context_window", 32000)),
                "timeout_seconds": float(declaration.get("timeout_seconds", 90.0)),
            }

    if unavailable:
        logger.info(
            "[hostile_reviewer] unavailable models (degraded run): %s",
            ", ".join(unavailable),
        )

    return configs


__all__: list[str] = ["build_model_configs"]
