# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelPolicyLoader — resolves model policy IDs to runtime URLs.

Reads model_policy.yaml from the omnimarket package root and resolves
each policy's env_var to a concrete URL at runtime. Raises RuntimeError
on missing env vars — no silent fallback to hardcoded IPs.

Related: OMN-8782
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

_POLICY_FILE = Path(__file__).parents[3] / "model_policy.yaml"


@lru_cache(maxsize=1)
def _load_policy_file() -> dict[str, Any]:
    if not _POLICY_FILE.exists():
        raise FileNotFoundError(
            f"model_policy.yaml not found at {_POLICY_FILE}. "
            "Create it at src/omnimarket/model_policy.yaml."
        )
    return yaml.safe_load(_POLICY_FILE.read_text())  # type: ignore[no-any-return]


class ModelPolicyLoader:
    """Resolves model policy IDs to endpoint URLs from environment variables.

    Usage:
        loader = ModelPolicyLoader()
        coder_url = loader.resolve("coder")      # reads LLM_CODER_URL
        judge_url = loader.resolve("judge")      # reads LLM_DEEPSEEK_R1_URL
    """

    def resolve(self, policy_id: str) -> str:
        """Resolve a policy ID to its base URL.

        Reads the env_var declared in model_policy.yaml for this policy
        and returns its value. Raises RuntimeError if not set.
        """
        data = _load_policy_file()
        policies: dict[str, Any] = data.get("policies", {})
        policy = policies.get(policy_id)
        if policy is None:
            raise RuntimeError(
                f"Unknown model policy ID {policy_id!r}. "
                f"Known policies: {list(policies.keys())}"
            )
        env_var: str = policy.get("env_var", "")
        if not env_var:
            raise RuntimeError(
                f"Policy {policy_id!r} has no env_var declared in model_policy.yaml."
            )
        url = os.environ.get(env_var, "")
        if not url:
            raise RuntimeError(
                f"Model endpoint for policy {policy_id!r} not configured. "
                f"Set {env_var} env var to an OpenAI-compatible base URL."
            )
        return url.rstrip("/")

    def resolve_optional(self, policy_id: str) -> str | None:
        """Resolve a policy ID to its base URL, returning None if not configured."""
        try:
            return self.resolve(policy_id)
        except RuntimeError:
            return None

    def resolve_api_key(self, policy_id: str) -> str:
        """Resolve the API key env var for a policy. Returns empty string for local models."""
        data = _load_policy_file()
        policies: dict[str, Any] = data.get("policies", {})
        policy = policies.get(policy_id)
        if policy is None:
            return ""
        api_key_env: str = policy.get("api_key_env_var", "")
        if not api_key_env:
            return ""
        return os.environ.get(api_key_env, "")

    def resolve_model_id(self, policy_id: str) -> str:
        """Resolve the model ID for a policy."""
        data = _load_policy_file()
        policies: dict[str, Any] = data.get("policies", {})
        policy = policies.get(policy_id)
        if policy is None:
            return "default"
        # Support env-var override for model ID
        model_id_env: str = policy.get("model_id_env_var", "")
        if model_id_env:
            val = os.environ.get(model_id_env, "")
            if val:
                return val
        return str(policy.get("model_id", policy.get("model_id_default", "default")))


__all__: list[str] = ["ModelPolicyLoader"]
