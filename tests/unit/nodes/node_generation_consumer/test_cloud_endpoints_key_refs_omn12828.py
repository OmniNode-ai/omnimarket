# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-12828 (B3) — cloud URLs + api-key references through bifrost.

B3 builds on A1 (OMN-12815: complete verbatim endpoints) and HG2 (OMN-12824:
secret-store resolver at the inference effect boundary). The remaining B3 work,
in omnimarket scope, is:

  1. The committed bifrost contract carries the COMPLETE verbatim ``endpoint_url``
     (full chat path) for every cloud backend (``cloud-gemini-flash``,
     ``openrouter-glm-flash``, ``openrouter-qwen3-coder-480b``) plus a declared
     API-key reference.
  2. Cloud-backend routability must NOT depend on the host process environment
     carrying the secret VALUE. The secret VALUE is resolved fail-closed at the
     effect boundary via the secret store (HG2). Reading ``os.environ`` to decide
     whether a backend is routable couples routing to a host-``.env`` runtime
     dependency that B3 removes.
  3. The resolver returns the contract ``endpoint_url`` VERBATIM (no in-code
     construction) for each cloud backend.
  4. Fail-closed when ``endpoint_url`` is empty — never a silent default.

These tests pin the contract values and the routing-authority behavior. The
secret-VALUE fail-closed path is already covered at the effect boundary by
``tests/unit/inference/test_secret_store_resolver.py`` (HG2).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_generation_consumer.handlers.handler_generation_consumer import (
    resolve_generation_endpoint,
)

_COMMITTED_BIFROST = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "omnimarket"
    / "configs"
    / "bifrost_delegation.yaml"
)

# The COMPLETE verbatim endpoints B3 requires (A1 supplied these).
_GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

_CLOUD_BACKENDS: dict[str, tuple[str, str]] = {
    # backend_id -> (expected endpoint_url, expected api-key reference name)
    "cloud-gemini-flash": (_GEMINI_URL, "GEMINI_API_KEY"),
    "openrouter-glm-flash": (_OPENROUTER_URL, "OPENROUTER_API_KEY"),
    "openrouter-qwen3-coder-480b": (_OPENROUTER_URL, "OPENROUTER_API_KEY"),
}


def _committed_backends() -> dict[str, dict[str, Any]]:
    data = yaml.safe_load(_COMMITTED_BIFROST.read_text(encoding="utf-8"))
    return {b["backend_id"]: b for b in data["backends"]}


# ---------------------------------------------------------------------------
# 1. Committed contract carries complete verbatim cloud endpoints + key refs.
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("backend_id", sorted(_CLOUD_BACKENDS))
def test_committed_contract_has_complete_endpoint_url(backend_id: str) -> None:
    """Every cloud backend declares the COMPLETE chat-completions URL verbatim."""
    backends = _committed_backends()
    expected_url, _ = _CLOUD_BACKENDS[backend_id]
    assert backend_id in backends, f"{backend_id} missing from committed contract"
    assert backends[backend_id]["endpoint_url"] == expected_url


@pytest.mark.unit
@pytest.mark.parametrize("backend_id", sorted(_CLOUD_BACKENDS))
def test_committed_contract_declares_api_key_reference(backend_id: str) -> None:
    """Every cloud backend declares its API-key reference NAME (never the value)."""
    backends = _committed_backends()
    _, expected_ref = _CLOUD_BACKENDS[backend_id]
    backend = backends[backend_id]
    # The contract may name the reference field ``api_key_ref`` (canonical) or
    # ``api_key_env`` (legacy core model). Either way it carries only the NAME.
    declared = backend.get("api_key_ref") or backend.get("api_key_env")
    assert declared == expected_ref


@pytest.mark.unit
def test_committed_contract_has_no_literal_api_key() -> None:
    """No literal secret VALUE appears in the committed contract.

    The contract carries only reference NAMES (``GEMINI_API_KEY`` /
    ``OPENROUTER_API_KEY``), never values. A real key would appear as a long
    provider-prefixed token (``sk-or-...`` / ``sk-...`` / ``AIza...``) — match
    those token shapes, not bare prose substrings like "task-class".
    """
    raw = _COMMITTED_BIFROST.read_text(encoding="utf-8")
    leaked = re.findall(r"\b(?:sk-[A-Za-z0-9-]{16,}|AIza[A-Za-z0-9_-]{16,})\b", raw)
    assert not leaked, f"possible literal secret token(s) in contract: {leaked}"


# ---------------------------------------------------------------------------
# 2/3. Resolver returns the contract endpoint VERBATIM for each cloud backend,
#      and routability does NOT depend on the host env carrying the secret.
# ---------------------------------------------------------------------------


def _set_committed_bifrost(monkeypatch: Any, tmp_path: Path) -> None:
    """Point the loader at the committed contract with a sentinel (no) overlay."""
    overlay_path = tmp_path / "__no_overlay__.yaml"
    monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(_COMMITTED_BIFROST))
    monkeypatch.setenv("BIFROST_OVERLAY_PATH", str(overlay_path))


@pytest.mark.unit
@pytest.mark.parametrize("backend_id", sorted(_CLOUD_BACKENDS))
def test_cloud_backend_routable_without_host_env_secret(
    backend_id: str, monkeypatch: Any, tmp_path: Path
) -> None:
    """A cloud backend is routable from the contract even when the secret VALUE
    is absent from the host environment.

    Routability is a contract property (endpoint_url + declared key ref); the
    secret VALUE resolves fail-closed at the effect boundary (HG2), not here.
    Coupling routability to ``os.environ`` is the host-``.env`` dependency B3
    removes.
    """
    expected_url, expected_ref = _CLOUD_BACKENDS[backend_id]
    # Explicitly clear the secret from the environment.
    monkeypatch.delenv(expected_ref, raising=False)
    _set_committed_bifrost(monkeypatch, tmp_path)

    resolved = resolve_generation_endpoint(
        endpoint_ref=backend_id,
        provider="gemini" if backend_id == "cloud-gemini-flash" else "openrouter",
        served_model_id="model-x",
    )
    # POST URL == contract endpoint_url, VERBATIM.
    assert resolved.endpoint_url == expected_url
    # The reference NAME is carried for the effect boundary to resolve the value.
    assert resolved.api_key_ref == expected_ref


@pytest.mark.unit
def test_resolver_fail_closed_when_endpoint_url_empty(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """A cloud backend with an empty endpoint_url fails closed — no default."""
    contract = {
        "config_version": "2.0.0",
        "schema_version": "bifrost_delegation.v1",
        "backends": [
            {
                "backend_id": "cloud-gemini-flash",
                "endpoint_url": "",
                "model_name": "gemini-2.0-flash",
                "api_key_env": "GEMINI_API_KEY",
                "tier": "cheap_cloud",
                "timeout_ms": 60000,
                "capabilities": ["simple_tasks"],
            }
        ],
        "routing_rules": [
            {
                "rule_id": "d4e5f6a7-0001-4000-8000-000000000001",
                "priority": 10,
                "task_class": "code_generation",
                "task_class_contract_version": "1.0.0",
                "backend_policy_version": "2.0.0",
                "match_operation_types": ["chat_completion"],
                "match_capabilities": ["simple_tasks"],
                "backend_ids": ["cloud-gemini-flash"],
                "fallback_policy": {
                    "action": "return_error",
                    "max_retries": 0,
                    "on_exhaust": "return_error",
                },
                "shadow_policy_id": "e5f6a7b8-0001-4000-8000-000000000001",
            }
        ],
        "default_backends": ["cloud-gemini-flash"],
    }
    contract_path = tmp_path / "bifrost_delegation.yaml"
    contract_path.write_text(yaml.dump(contract))
    overlay_path = tmp_path / "__no_overlay__.yaml"
    monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(contract_path))
    monkeypatch.setenv("BIFROST_OVERLAY_PATH", str(overlay_path))

    with pytest.raises(ValueError, match=r"endpoint_ref|backend|endpoint_url"):
        resolve_generation_endpoint(
            endpoint_ref="cloud-gemini-flash",
            provider="gemini",
            served_model_id="gemini-2.0-flash",
        )
