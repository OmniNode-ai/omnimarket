# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-12801 — generation endpoint resolution from the routing authority.

Wave 1B completion: the generation consumer must resolve
``endpoint_url + provider + served_model_id + api_key_ref`` per-model from the
routing authority (bifrost delegation contract overlay keyed by the contract's
``endpoint_ref``), NOT from the shared ``LLM_CODER_URL`` env var.

These tests mirror ``node_delegation_routing_reducer.delta()`` authority:
endpoint URLs come from the bifrost contract overlay, api_key references come
from the backend ``api_key_env`` declaration, and the served model id comes
from the contract ``served_model_id`` (the routing-tier authority value).

Hard requirements proven here:
  * No ``os.environ`` read of an endpoint env var anywhere in the resolution path.
  * Fail-closed: any missing field of the four raises, never a silent default.
  * Provider-agnostic: local vLLM and Gemini resolve to distinct URLs.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_generation_consumer.handlers.handler_generation_consumer import (
    HandlerGenerationConsumer,
    resolve_generation_endpoint,
)

# ---------------------------------------------------------------------------
# Bifrost contract fixtures — endpoint_url + api_key_env per provider.
# ---------------------------------------------------------------------------

_LOCAL_VLLM_URL = "http://100.109.203.94:8000/v1/chat/completions"  # onex-allow-internal-ip OMN-12801 reason="test fixture endpoint URL for routing-authority resolution"
_GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"


def _bifrost_contract(
    *,
    local_endpoint: str | None,
    gemini_endpoint: str | None,
    local_model_name: str | None = "qwen-coder",
    local_max_tokens: int = 65536,
    gemini_max_tokens: int = 8192,
) -> str:
    """Build a bifrost delegation contract with local + gemini backends."""
    return textwrap.dedent(
        f"""\
        config_version: "2.0.0"
        schema_version: "bifrost_delegation.v1"
        backends:
          - backend_id: local-coder
            endpoint_url: {"null" if local_endpoint is None else f'"{local_endpoint}"'}
            model_name: {"null" if local_model_name is None else f'"{local_model_name}"'}
            tier: local
            timeout_ms: 60000
            max_tokens: {local_max_tokens}
            capabilities: [code_generation]
          - backend_id: cloud-gemini-flash
            endpoint_url: {"null" if gemini_endpoint is None else f'"{gemini_endpoint}"'}
            model_name: "gemini-2.0-flash"
            api_key_env: GEMINI_API_KEY
            tier: cheap_cloud
            timeout_ms: 60000
            max_tokens: {gemini_max_tokens}
            capabilities: [code_generation]
        routing_rules:
          - rule_id: "d4e5f6a7-0001-4000-8000-000000000001"
            priority: 10
            task_class: code_generation
            task_class_contract_version: "1.0.0"
            backend_policy_version: "2.0.0"
            match_operation_types: [chat_completion]
            match_capabilities: [code_generation]
            backend_ids: [local-coder, cloud-gemini-flash]
            fallback_policy:
              action: escalate_to_next_tier
              max_retries: 1
              on_exhaust: return_error
            shadow_policy_id: "e5f6a7b8-0001-4000-8000-000000000001"
        default_backends: [local-coder, cloud-gemini-flash]
        """
    )


def _write_contract(
    tmp_path: Path,
    *,
    endpoint_ref: str,
    provider: str,
    served_model_id: str,
) -> Path:
    contract = {
        "name": "node_generation_consumer",
        "contract_version": {"major": 1, "minor": 0, "patch": 0},
        "node_type": "orchestrator",
        "node_version": {"major": 1, "minor": 0, "patch": 0},
        "event_bus": {"publish_topics": [], "subscribe_topics": []},
        "model_routing": {
            "provider": provider,
            "served_model_id": served_model_id,
            "endpoint_ref": endpoint_ref,
            "routing_source": "contract",
        },
    }
    contract_path = tmp_path / f"contract-{endpoint_ref}.yaml"
    contract_path.write_text(yaml.dump(contract))
    return contract_path


def _set_bifrost(monkeypatch: Any, tmp_path: Path, contract_yaml: str) -> None:
    """Point the bifrost loader at our fixture contract with no overlay."""
    contract_path = tmp_path / "bifrost_delegation.yaml"
    contract_path.write_text(contract_yaml)
    # Sentinel overlay path so the loader does NOT merge the developer's
    # ~/.omninode overlay into the deterministic fixture.
    overlay_path = tmp_path / "__no_overlay__.yaml"
    monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(contract_path))
    monkeypatch.setenv("BIFROST_OVERLAY_PATH", str(overlay_path))


# ---------------------------------------------------------------------------
# resolve_generation_endpoint — the routing-authority resolver under test.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resolves_local_vllm_endpoint_from_authority(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """Local backend resolves endpoint_url from the bifrost authority, api_key None."""
    _set_bifrost(
        monkeypatch,
        tmp_path,
        _bifrost_contract(local_endpoint=_LOCAL_VLLM_URL, gemini_endpoint=None),
    )
    resolved = resolve_generation_endpoint(
        endpoint_ref="local-coder",
        provider="local",
        served_model_id="Qwen3.6-35B-A3B",
    )
    assert resolved.endpoint_url == _LOCAL_VLLM_URL
    assert resolved.provider == "local"
    assert resolved.served_model_id == "Qwen3.6-35B-A3B"
    assert resolved.api_key_ref is None


@pytest.mark.unit
def test_resolves_gemini_endpoint_from_authority(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """Gemini backend resolves a distinct endpoint_url + api_key_ref."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    _set_bifrost(
        monkeypatch,
        tmp_path,
        _bifrost_contract(local_endpoint=None, gemini_endpoint=_GEMINI_URL),
    )
    resolved = resolve_generation_endpoint(
        endpoint_ref="cloud-gemini-flash",
        provider="gemini",
        served_model_id="gemini-2.0-flash",
    )
    assert resolved.endpoint_url == _GEMINI_URL
    assert resolved.provider == "gemini"
    assert resolved.served_model_id == "gemini-2.0-flash"
    assert resolved.api_key_ref == "GEMINI_API_KEY"


@pytest.mark.unit
def test_two_providers_resolve_to_distinct_urls(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """Provider-agnostic: local vLLM and Gemini resolve to different URLs (never one base)."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    _set_bifrost(
        monkeypatch,
        tmp_path,
        _bifrost_contract(local_endpoint=_LOCAL_VLLM_URL, gemini_endpoint=_GEMINI_URL),
    )
    local = resolve_generation_endpoint(
        endpoint_ref="local-coder",
        provider="local",
        served_model_id="Qwen3.6-35B-A3B",
    )
    gemini = resolve_generation_endpoint(
        endpoint_ref="cloud-gemini-flash",
        provider="gemini",
        served_model_id="gemini-2.0-flash",
    )
    assert local.endpoint_url != gemini.endpoint_url
    assert local.endpoint_url == _LOCAL_VLLM_URL
    assert gemini.endpoint_url == _GEMINI_URL


# ---------------------------------------------------------------------------
# Fail-closed: each of the four fields must raise when unresolvable.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_fail_closed_when_endpoint_url_missing(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """No endpoint_url in the authority for the backend → raise, never default."""
    _set_bifrost(
        monkeypatch,
        tmp_path,
        _bifrost_contract(local_endpoint=None, gemini_endpoint=None),
    )
    with pytest.raises(ValueError, match=r"endpoint_url"):
        resolve_generation_endpoint(
            endpoint_ref="local-coder",
            provider="local",
            served_model_id="Qwen3.6-35B-A3B",
        )


@pytest.mark.unit
def test_fail_closed_when_backend_ref_unknown(monkeypatch: Any, tmp_path: Path) -> None:
    """endpoint_ref not declared in the authority → raise."""
    _set_bifrost(
        monkeypatch,
        tmp_path,
        _bifrost_contract(local_endpoint=_LOCAL_VLLM_URL, gemini_endpoint=None),
    )
    with pytest.raises(ValueError, match=r"endpoint_ref|backend"):
        resolve_generation_endpoint(
            endpoint_ref="does-not-exist",
            provider="local",
            served_model_id="Qwen3.6-35B-A3B",
        )


@pytest.mark.unit
def test_fail_closed_when_provider_blank(monkeypatch: Any, tmp_path: Path) -> None:
    """Blank provider → raise (no silent default)."""
    _set_bifrost(
        monkeypatch,
        tmp_path,
        _bifrost_contract(local_endpoint=_LOCAL_VLLM_URL, gemini_endpoint=None),
    )
    with pytest.raises(ValueError, match=r"provider"):
        resolve_generation_endpoint(
            endpoint_ref="local-coder",
            provider="",
            served_model_id="Qwen3.6-35B-A3B",
        )


@pytest.mark.unit
def test_fail_closed_when_served_model_id_blank(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """Blank served_model_id → raise (no silent default)."""
    _set_bifrost(
        monkeypatch,
        tmp_path,
        _bifrost_contract(local_endpoint=_LOCAL_VLLM_URL, gemini_endpoint=None),
    )
    with pytest.raises(ValueError, match=r"served_model_id"):
        resolve_generation_endpoint(
            endpoint_ref="local-coder",
            provider="local",
            served_model_id="",
        )


@pytest.mark.unit
def test_routable_when_gemini_api_key_absent_from_host_env(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """OMN-12828 (B3): a cloud backend is routable from the contract even when the
    secret VALUE is absent from the host environment.

    Routability is a contract property (endpoint_url + declared key REFERENCE);
    the secret VALUE resolves fail-closed at the effect boundary (HG2), not in
    the routing resolver. This removes the host-``.env`` runtime dependency that
    previously skipped the backend when its key env var was unset.
    """
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    _set_bifrost(
        monkeypatch,
        tmp_path,
        _bifrost_contract(local_endpoint=None, gemini_endpoint=_GEMINI_URL),
    )
    resolved = resolve_generation_endpoint(
        endpoint_ref="cloud-gemini-flash",
        provider="gemini",
        served_model_id="gemini-2.0-flash",
    )
    assert resolved.endpoint_url == _GEMINI_URL
    assert resolved.api_key_ref == "GEMINI_API_KEY"


@pytest.mark.unit
def test_routable_when_gemini_api_key_blank_in_host_env(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """OMN-12828 (B3): a blank host env var no longer makes the backend unroutable.

    The reference NAME is carried through; the effect boundary resolves the value
    fail-closed (a blank value raises there), so routing stays contract-driven.
    """
    monkeypatch.setenv("GEMINI_API_KEY", "   ")
    _set_bifrost(
        monkeypatch,
        tmp_path,
        _bifrost_contract(local_endpoint=None, gemini_endpoint=_GEMINI_URL),
    )
    resolved = resolve_generation_endpoint(
        endpoint_ref="cloud-gemini-flash",
        provider="gemini",
        served_model_id="gemini-2.0-flash",
    )
    assert resolved.endpoint_url == _GEMINI_URL
    assert resolved.api_key_ref == "GEMINI_API_KEY"


# ---------------------------------------------------------------------------
# OMN-13342 — the contract-declared per-backend max_tokens (output ceiling)
# MUST be threaded onto the resolved endpoint and the inference request. When
# omitted, z.ai glm-4.5 truncates at its small server-side default
# (finish_reason=length) and the quality gate scores 0.0.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resolved_endpoint_carries_backend_max_tokens(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """The resolved endpoint carries the contract-declared backend output ceiling.

    Regression for OMN-13342: the bifrost backend max_tokens was read then
    discarded (_ResolvedBackend / ModelResolvedEndpoint had no field), so the
    generation inference request omitted max_tokens and cloud providers
    truncated. The resolved endpoint must surface the contract value verbatim,
    not the wire-DTO default — proven here with a non-default explicit value.
    """
    _set_bifrost(
        monkeypatch,
        tmp_path,
        _bifrost_contract(
            local_endpoint=_LOCAL_VLLM_URL,
            gemini_endpoint=None,
            local_max_tokens=12345,
        ),
    )
    resolved = resolve_generation_endpoint(
        endpoint_ref="local-coder",
        provider="local",
        served_model_id="Qwen3.6-35B-A3B",
    )
    assert resolved.max_tokens == 12345


@pytest.mark.unit
def test_cloud_backend_resolves_full_output_ceiling(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """A cloud backend resolves its full output ceiling (the truncation guard).

    Mirrors the cloud-glm case: a 65536 ceiling must reach the resolved
    endpoint so the inference effect posts max_tokens on the wire and z.ai
    glm-4.5 stops truncating at finish_reason=length.
    """
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    _set_bifrost(
        monkeypatch,
        tmp_path,
        _bifrost_contract(
            local_endpoint=None,
            gemini_endpoint=_GEMINI_URL,
            gemini_max_tokens=65536,
        ),
    )
    resolved = resolve_generation_endpoint(
        endpoint_ref="cloud-gemini-flash",
        provider="gemini",
        served_model_id="gemini-2.0-flash",
    )
    assert resolved.max_tokens == 65536


@pytest.mark.unit
def test_production_cloud_glm_resolves_65536_ceiling(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """The real production cloud-glm backend resolves a 65536 output ceiling.

    Guards against the canonical bifrost config (configs/bifrost_delegation.yaml)
    dropping or shrinking the cloud-glm max_tokens, which would re-open the
    z.ai glm-4.5 truncation. Loads the production config with a sentinel overlay
    so the developer's ~/.omninode overlay does not perturb the assertion.
    """
    from omnimarket.adapters.llm.bifrost.config_loader_bifrost_delegation import (
        _DEFAULT_CONFIG_PATH,
    )

    overlay_path = tmp_path / "__no_overlay__.yaml"
    monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(_DEFAULT_CONFIG_PATH))
    monkeypatch.setenv("BIFROST_OVERLAY_PATH", str(overlay_path))
    resolved = resolve_generation_endpoint(
        endpoint_ref="cloud-glm",
        provider="cloud",
        served_model_id="glm-4.5",
    )
    assert resolved.max_tokens == 65536


@pytest.mark.unit
def test_fail_closed_when_backend_max_tokens_non_positive(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """A non-positive contract output ceiling raises — no silent default.

    The wire DTO bounds max_tokens >= 1, but the resolver also fails closed at
    the resolution boundary (mirrors delegation_backend_resolution.py). A
    fixture declaring max_tokens: 0 must be rejected before it can produce a
    truncating wire request.
    """
    contract_yaml = textwrap.dedent(
        f"""\
        config_version: "2.0.0"
        schema_version: "bifrost_delegation.v1"
        backends:
          - backend_id: local-coder
            endpoint_url: "{_LOCAL_VLLM_URL}"
            model_name: "qwen-coder"
            tier: local
            timeout_ms: 60000
            max_tokens: 0
            capabilities: [code_generation]
        routing_rules:
          - rule_id: "d4e5f6a7-0001-4000-8000-000000000001"
            priority: 10
            task_class: code_generation
            task_class_contract_version: "1.0.0"
            backend_policy_version: "2.0.0"
            match_operation_types: [chat_completion]
            match_capabilities: [code_generation]
            backend_ids: [local-coder]
            fallback_policy:
              action: escalate_to_next_tier
              max_retries: 1
              on_exhaust: return_error
            shadow_policy_id: "e5f6a7b8-0001-4000-8000-000000000001"
        default_backends: [local-coder]
        """
    )
    _set_bifrost(monkeypatch, tmp_path, contract_yaml)
    with pytest.raises(ValueError, match=r"max_tokens"):
        resolve_generation_endpoint(
            endpoint_ref="local-coder",
            provider="local",
            served_model_id="Qwen3.6-35B-A3B",
        )


# ---------------------------------------------------------------------------
# No env-var endpoint indirection anywhere in the resolution path.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_no_llm_coder_url_env_read(monkeypatch: Any, tmp_path: Path) -> None:
    """Resolution must NOT consult LLM_CODER_URL — even when it is set to a wrong value."""
    monkeypatch.setenv("LLM_CODER_URL", "http://wrong.example:9999")
    _set_bifrost(
        monkeypatch,
        tmp_path,
        _bifrost_contract(local_endpoint=_LOCAL_VLLM_URL, gemini_endpoint=None),
    )
    resolved = resolve_generation_endpoint(
        endpoint_ref="local-coder",
        provider="local",
        served_model_id="Qwen3.6-35B-A3B",
    )
    assert resolved.endpoint_url == _LOCAL_VLLM_URL
    assert "wrong.example" not in resolved.endpoint_url


@pytest.mark.unit
def test_handler_source_has_no_endpoint_env_read() -> None:
    """The handler module must not read an endpoint URL from os.environ."""
    from omnimarket.nodes.node_generation_consumer.handlers import (
        handler_generation_consumer as mod,
    )

    source = Path(mod.__file__).read_text()
    # The deleted env-indirection code surface must be gone entirely.
    assert "os.environ[self._endpoint_env]" not in source
    assert "self._endpoint_env" not in source
    assert "self._endpoint_mode" not in source
    assert "_MODEL_ROUTING_ENDPOINT_ENV_KEY" not in source
    assert "_MODEL_ROUTING_ENDPOINT_MODE_KEY" not in source


# ---------------------------------------------------------------------------
# Production contract must NOT carry the deleted env-indirection keys.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_production_contract_drops_endpoint_env_and_mode() -> None:
    """contract.yaml must no longer declare endpoint_env / endpoint_mode / LLM_CODER_URL."""
    from omnimarket.nodes.node_generation_consumer.handlers.handler_generation_consumer import (
        _CONTRACT_PATH,
    )

    contract = yaml.safe_load(_CONTRACT_PATH.read_text())
    model_routing = contract.get("model_routing", {})
    assert "endpoint_env" not in model_routing
    assert "endpoint_mode" not in model_routing
    # The contract-declared routing-authority fields remain.
    assert model_routing.get("provider")
    assert model_routing.get("served_model_id")
    assert model_routing.get("endpoint_ref")
    # The deleted LLM_CODER_URL env dependency must be gone — endpoint URL is
    # resolved from the routing authority, not an env var dependency.
    env_keys = {
        dep["key"]
        for dep in contract.get("dependencies", [])
        if isinstance(dep, dict) and dep.get("type") == "environment" and "key" in dep
    }
    assert "LLM_CODER_URL" not in env_keys


@pytest.mark.unit
def test_handler_constructs_with_production_contract() -> None:
    """The real production contract must construct the handler without the env keys."""
    handler = HandlerGenerationConsumer(
        effect_handler=object(),  # never invoked at construction
    )
    assert handler._provider == "local"
    assert handler._endpoint_ref == "local-coder"
    assert handler._served_model_id
