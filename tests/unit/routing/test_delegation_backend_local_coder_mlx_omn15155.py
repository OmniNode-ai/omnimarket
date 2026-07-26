# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""`local-coder-mlx` backend registration tests (OMN-15155).

Registers the .200 MLX endpoint (mlx_lm.server, unauthenticated) as the
committed ``local-coder-mlx`` backend_id in ``bifrost_delegation.yaml``, per
the steel-node-dispatch integration plan P0
(``omni_home/docs/plans/2026-07-26-steel-node-dispatch-integration-plan.md``).

DoD evidence required by OMN-15155:
  1. The committed contract declares ``local-coder-mlx`` with a null
     ``endpoint_url``, ``model_name: mlx-community/Qwen3.6-35B-A3B-8bit``,
     ``tier: local``, and no ``secret_ref``/``api_key_env`` (verified
     unauthenticated mlx_lm.server, 2026-07-26).
  2. A stability-test-shaped overlay/store entry for ``local-coder-mlx``
     survives ``_merge_overlay`` and resolves via the public
     ``resolve_delegation_backend`` entrypoint to the COMPLETE chat-completions
     URL ``http://stickybeatz-studio:8401/v1/chat/completions`` (never the bare
     ``/v1`` base — the named silent-failure class).
  3. Proof that the committed-file entry is *mandatory*: an overlay-only
     ``backend_id`` with no matching committed entry is silently dropped by
     ``_merge_overlay`` (the exact failure mode this ticket's committed-file
     entry avoids for ``local-coder-mlx``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.models.delegation.wire.model_bifrost_delegation_config import (
    ModelDelegationBackendConfig,
)
from omnimarket.routing.delegation_backend_resolution import (
    BIFROST_OVERLAY_STORE_KEY,
    load_bifrost_backends,
    resolve_delegation_backend,
)

_BIFROST_CONFIG_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "configs"
    / "bifrost_delegation.yaml"
)

_STABILITY_TEST_ENDPOINT = "http://stickybeatz-studio:8401/v1/chat/completions"


class _MockStore:
    """Minimal in-memory ProtocolSecretStore for unit tests."""

    def __init__(self, data: dict[str, str]) -> None:
        self._data = data

    async def get_secret(self, key: str) -> str | None:
        return self._data.get(key)

    async def set_secret(self, key: str, value: str) -> bool:
        raise RuntimeError("Mock store is read-only")

    async def delete_secret(self, key: str) -> bool:
        raise RuntimeError("Mock store is read-only")

    async def list_keys(self, prefix: str | None = None) -> list[str]:
        del prefix
        return list(self._data.keys())

    async def health_check(self) -> bool:
        return True

    async def close(self, timeout_seconds: float = 30.0) -> None:
        del timeout_seconds


def _overlay_yaml(backends: list[dict[str, Any]]) -> str:
    return yaml.dump({"backends": backends}, default_flow_style=False)


# ---------------------------------------------------------------------------
# 1. Committed contract declares local-coder-mlx correctly
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_committed_contract_declares_local_coder_mlx() -> None:
    raw = yaml.safe_load(_BIFROST_CONFIG_PATH.read_text(encoding="utf-8"))
    backends = {backend["backend_id"]: backend for backend in raw["backends"]}

    assert "local-coder-mlx" in backends, (
        "local-coder-mlx must be a COMMITTED backend_id — _merge_overlay silently "
        "drops overlay-only backend_ids (OMN-15155)."
    )
    backend = backends["local-coder-mlx"]

    assert backend["model_name"] == "mlx-community/Qwen3.6-35B-A3B-8bit"
    assert backend["tier"] == "local"
    # Committed default is null; the real endpoint is supplied by the
    # stability-test overlay/store, never hardcoded here.
    assert backend["endpoint_url"] is None
    assert backend["endpoint_url_env"] == "BIFROST_LOCAL_CODER_MLX_ENDPOINT_URL"

    # Verified unauthenticated mlx_lm.server (2026-07-26): no auth fields.
    assert backend.get("secret_ref") is None
    assert backend.get("api_key_env") is None
    assert backend.get("api_key_ref") is None


@pytest.mark.unit
def test_committed_local_coder_mlx_validates_against_wire_model() -> None:
    """The committed entry must validate against the strict wire DTO."""
    raw = yaml.safe_load(_BIFROST_CONFIG_PATH.read_text(encoding="utf-8"))
    backend = next(b for b in raw["backends"] if b["backend_id"] == "local-coder-mlx")
    config = ModelDelegationBackendConfig.model_validate(backend)
    assert config.timeout_ms >= 1
    assert config.max_tokens >= 1
    assert config.resolved_secret_ref is None


# ---------------------------------------------------------------------------
# 2. Stability-test overlay/store survives _merge_overlay and resolves the
#    COMPLETE chat-completions URL via resolve_delegation_backend.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_stability_test_store_overlay_merges_onto_committed_local_coder_mlx() -> None:
    """A stability-test-shaped store overlay merges field-by-field onto the
    committed local-coder-mlx entry (same overlay/store mechanism the sibling
    local backends — e.g. local-coder, local-ds-v4-flash — use)."""
    store = _MockStore(
        {
            BIFROST_OVERLAY_STORE_KEY: _overlay_yaml(
                [
                    {
                        "backend_id": "local-coder-mlx",
                        "endpoint_url": _STABILITY_TEST_ENDPOINT,
                    }
                ]
            )
        }
    )

    backends = load_bifrost_backends(config_path=_BIFROST_CONFIG_PATH, store=store)
    mlx_backend = next(b for b in backends if b["backend_id"] == "local-coder-mlx")

    assert mlx_backend["endpoint_url"] == _STABILITY_TEST_ENDPOINT
    # Committed fields not touched by the overlay survive the merge verbatim.
    assert mlx_backend["model_name"] == "mlx-community/Qwen3.6-35B-A3B-8bit"
    assert mlx_backend["tier"] == "local"


@pytest.mark.unit
def test_resolve_delegation_backend_local_coder_mlx_ends_in_chat_completions() -> None:
    """End-to-end via the public resolve_delegation_backend entrypoint: the
    resolved endpoint_ref is the COMPLETE chat-completions path, never the bare
    /v1 base (the named silent-failure class this ticket guards against)."""
    store = _MockStore(
        {
            BIFROST_OVERLAY_STORE_KEY: _overlay_yaml(
                [
                    {
                        "backend_id": "local-coder-mlx",
                        "endpoint_url": _STABILITY_TEST_ENDPOINT,
                    }
                ]
            )
        }
    )

    resolved = resolve_delegation_backend(
        "code_generation",
        backend_id="local-coder-mlx",
        config_path=_BIFROST_CONFIG_PATH,
        store=store,
    )

    assert resolved.backend_id == "local-coder-mlx"
    assert resolved.endpoint_ref == _STABILITY_TEST_ENDPOINT
    assert resolved.endpoint_ref.endswith("/v1/chat/completions")
    assert resolved.model_id == "mlx-community/Qwen3.6-35B-A3B-8bit"
    assert resolved.tier == "local"
    assert resolved.secret_ref is None
    assert resolved.api_key_env is None


@pytest.mark.unit
def test_resolve_delegation_backend_rejects_bare_v1_base_class_of_failure() -> None:
    """A bare-base URL (no /chat/completions path) is carried verbatim by the
    resolver — never silently constructed into a complete path — so a
    misconfigured overlay is visibly wrong rather than a silent failure."""
    bare_base = "http://stickybeatz-studio:8401/v1"
    store = _MockStore(
        {
            BIFROST_OVERLAY_STORE_KEY: _overlay_yaml(
                [{"backend_id": "local-coder-mlx", "endpoint_url": bare_base}]
            )
        }
    )

    resolved = resolve_delegation_backend(
        "code_generation",
        backend_id="local-coder-mlx",
        config_path=_BIFROST_CONFIG_PATH,
        store=store,
    )

    # Carried verbatim, not silently completed — proves the resolver performs
    # no in-code path construction (OMN-12815). A bare base is a
    # misconfiguration this test documents, not a code path this ticket wires.
    assert resolved.endpoint_ref == bare_base
    assert not resolved.endpoint_ref.endswith("/v1/chat/completions")


# ---------------------------------------------------------------------------
# 3. Proof the committed-file entry is mandatory: overlay-only backend_ids are
#    silently DROPPED by _merge_overlay.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_overlay_only_backend_id_is_silently_dropped_without_committed_entry() -> None:
    """Reproduces the exact failure mode OMN-15155's committed entry avoids:
    an overlay-only backend_id with no matching committed entry never appears
    in the merged config, so resolve_delegation_backend(backend_id=...) fails
    closed with "No delegation backend ... found" rather than silently
    resolving a phantom backend."""
    store = _MockStore(
        {
            BIFROST_OVERLAY_STORE_KEY: _overlay_yaml(
                [
                    {
                        "backend_id": "local-coder-mlx-not-committed",
                        "endpoint_url": _STABILITY_TEST_ENDPOINT,
                        "model_name": "mlx-community/Qwen3.6-35B-A3B-8bit",
                    }
                ]
            )
        }
    )

    backends = load_bifrost_backends(config_path=_BIFROST_CONFIG_PATH, store=store)
    ids = {b["backend_id"] for b in backends}
    assert "local-coder-mlx-not-committed" not in ids, (
        "_merge_overlay must silently drop an overlay-only backend_id that has "
        "no matching committed entry — this is why local-coder-mlx MUST be "
        "committed in bifrost_delegation.yaml, not overlay-only (OMN-15155)."
    )

    with pytest.raises(RuntimeError, match="local-coder-mlx-not-committed"):
        resolve_delegation_backend(
            "code_generation",
            backend_id="local-coder-mlx-not-committed",
            config_path=_BIFROST_CONFIG_PATH,
            store=store,
        )
