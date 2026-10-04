# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelPolicyLoader — resolves model policy IDs to runtime URLs.

Reads model_policy.yaml from the omnimarket package root and resolves
Bifrost-backed policies through the canonical contract and overlay. Other
policies use declared env vars. Parked endpoints never fall back to an env URL.

Related: OMN-8782
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from omnimarket.inference.bridge_config_loader import resolve_bifrost_backend

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
    """Resolves model policy IDs to contract endpoints or declared env URLs.

    Usage:
        loader = ModelPolicyLoader()
        coder_url = loader.resolve("coder")      # Bifrost local-coder endpoint
        judge_url = loader.resolve("judge")      # reads LLM_DEEPSEEK_R1_URL
    """

    def resolve(self, policy_id: str) -> str:
        """Resolve a policy ID to its endpoint URL.

        Bifrost policies return complete request URLs; env policies retain
        their declared URL form. Raises RuntimeError for an absent endpoint.
        """
        data = _load_policy_file()
        policies: dict[str, Any] = data.get("policies", {})
        policy = policies.get(policy_id)
        if policy is None:
            raise RuntimeError(
                f"Unknown model policy ID {policy_id!r}. "
                f"Known policies: {list(policies.keys())}"
            )
        backend_id: str = policy.get("bifrost_backend_id", "")
        if backend_id:
            backend = resolve_bifrost_backend(backend_id)
            if backend is None or not backend.endpoint_url:
                raise RuntimeError(
                    f"Model endpoint for policy {policy_id!r} not configured: "
                    f"Bifrost backend {backend_id!r} is absent or parked."
                )
            return backend.endpoint_url
        env_var: str = policy.get("env_var", "")
        endpoint_ref: str = policy.get("endpoint_ref", "")
        if not env_var and endpoint_ref.startswith("env:"):
            env_var = endpoint_ref.removeprefix("env:")
        if not env_var:
            raise RuntimeError(
                f"Policy {policy_id!r} has no env_var or env: endpoint_ref declared "
                "in model_policy.yaml."
            )
        url = os.environ.get(env_var, "")
        if not url:
            raise RuntimeError(
                f"Model endpoint for policy {policy_id!r} not configured. "
                f"Set {env_var} env var to an OpenAI-compatible base URL."
            )
        return url.rstrip("/")

    def resolve_optional(self, policy_id: str) -> str | None:
        """Resolve a policy ID to its endpoint URL, returning None if parked."""
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
        """Resolve the served model ID from Bifrost or the declared env var."""
        data = _load_policy_file()
        policies: dict[str, Any] = data.get("policies", {})
        policy = policies.get(policy_id)
        if policy is None:
            raise RuntimeError(
                f"Unknown model policy ID {policy_id!r}. "
                f"Known policies: {list(policies.keys())}"
            )
        backend_id: str = policy.get("bifrost_backend_id", "")
        if backend_id:
            backend = resolve_bifrost_backend(backend_id)
            if backend is None or not backend.model_name:
                raise RuntimeError(
                    f"Served model ID for policy {policy_id!r} not configured "
                    f"in Bifrost backend {backend_id!r}."
                )
            return backend.model_name
        model_id_env: str = policy.get("model_id_env_var", "")
        if not model_id_env:
            raise RuntimeError(
                f"Policy {policy_id!r} has no model_id_env_var declared in "
                "model_policy.yaml."
            )
        model_id = os.environ.get(model_id_env, "")
        if not model_id:
            raise RuntimeError(
                f"Served model ID for policy {policy_id!r} not configured. "
                f"Set {model_id_env} env var."
            )
        return model_id

    def resolve_model_id_optional(self, policy_id: str) -> str | None:
        """Resolve a policy's served model ID, returning None if not configured."""
        try:
            return self.resolve_model_id(policy_id)
        except RuntimeError:
            return None


__all__: list[str] = ["ModelPolicyLoader"]
