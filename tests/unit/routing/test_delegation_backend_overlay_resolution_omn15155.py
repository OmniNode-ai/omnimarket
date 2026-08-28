# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Committed-backend + overlay resolution tests (OMN-15155 / OMN-16442).

OMN-15155 registered the .200 MLX endpoint as the committed ``local-coder-mlx``
backend_id in ``bifrost_delegation.yaml`` and proved three general properties
through it. OMN-16442 RETIRED that backend — .200:8401 was re-probed 2026-08-28
and returns curl exit 7 "Couldn't connect to server", and the Mac Studio's MLX
server now serves ``Qwen3.8-27B-8bit`` on 127.0.0.1:8099, LOCALHOST-ONLY (so it
is deliberately NOT re-registered; that needs explicit availability semantics +
a health check first).

The three properties are NOT specific to that backend, so these tests were
retargeted onto ``local-ds-v4-flash`` — the surviving local backend with the
identical shape (``endpoint_url: null`` in the committed contract, supplied by
the overlay/store at deploy time, unauthenticated, ``tier: local``) — rather
than deleted with the backend:

  1. The committed contract declares the backend with a null ``endpoint_url``,
     its served ``model_name``, ``tier: local``, and no ``secret_ref`` /
     ``api_key_env``.
  2. A stability-test-shaped overlay/store entry survives ``_merge_overlay``
     and resolves via the public ``resolve_delegation_backend`` entrypoint to a
     COMPLETE chat-completions URL, never the bare ``/v1`` base — the named
     silent-failure class (OMN-12815).
  3. The committed-file entry is MANDATORY: an overlay-only ``backend_id`` with
     no matching committed entry is silently dropped by ``_merge_overlay``.
     This is the property that makes retiring a backend a two-file change, and
     it is exactly why OMN-16442 removed the retired ids from the committed
     contract rather than only from the overlay.
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

_STABILITY_TEST_ENDPOINT = "http://stickybeatz-studio:8101/v1/chat/completions"


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
# 1. Committed contract declares local-ds-v4-flash correctly
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_committed_contract_declares_local_ds_v4_flash() -> None:
    raw = yaml.safe_load(_BIFROST_CONFIG_PATH.read_text(encoding="utf-8"))
    backends = {backend["backend_id"]: backend for backend in raw["backends"]}

    assert "local-ds-v4-flash" in backends, (
        "local-ds-v4-flash must be a COMMITTED backend_id — _merge_overlay silently "
        "drops overlay-only backend_ids (OMN-15155)."
    )
    backend = backends["local-ds-v4-flash"]

    assert backend["model_name"] == "deepseek-v4-flash"
    assert backend["tier"] == "local"
    # Committed default is null; the real endpoint is supplied by the
    # stability-test overlay/store, never hardcoded here.
    assert backend["endpoint_url"] is None
    assert backend["endpoint_url_env"] == "BIFROST_LOCAL_DS_V4_FLASH_ENDPOINT_URL"

    # Unauthenticated local endpoint: no auth fields (OMN-16442: same
    # property held for the retired MLX backend this test was retargeted from).
    assert backend.get("secret_ref") is None
    assert backend.get("api_key_env") is None
    assert backend.get("api_key_ref") is None


@pytest.mark.unit
def test_committed_local_ds_v4_flash_validates_against_wire_model() -> None:
    """The committed entry must validate against the strict wire DTO."""
    raw = yaml.safe_load(_BIFROST_CONFIG_PATH.read_text(encoding="utf-8"))
    backend = next(b for b in raw["backends"] if b["backend_id"] == "local-ds-v4-flash")
    config = ModelDelegationBackendConfig.model_validate(backend)
    assert config.timeout_ms >= 1
    assert config.max_tokens >= 1
    assert config.resolved_secret_ref is None


# ---------------------------------------------------------------------------
# 2. Stability-test overlay/store survives _merge_overlay and resolves the
#    COMPLETE chat-completions URL via resolve_delegation_backend.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_stability_test_store_overlay_merges_onto_committed_local_ds_v4_flash() -> None:
    """A stability-test-shaped store overlay merges field-by-field onto the
    committed local-ds-v4-flash entry (same overlay/store mechanism the sibling
    local backends — e.g. local-coder, local-ds-v4-flash — use)."""
    store = _MockStore(
        {
            BIFROST_OVERLAY_STORE_KEY: _overlay_yaml(
                [
                    {
                        "backend_id": "local-ds-v4-flash",
                        "endpoint_url": _STABILITY_TEST_ENDPOINT,
                    }
                ]
            )
        }
    )

    backends = load_bifrost_backends(config_path=_BIFROST_CONFIG_PATH, store=store)
    ds_backend = next(b for b in backends if b["backend_id"] == "local-ds-v4-flash")

    assert ds_backend["endpoint_url"] == _STABILITY_TEST_ENDPOINT
    # Committed fields not touched by the overlay survive the merge verbatim.
    assert ds_backend["model_name"] == "deepseek-v4-flash"
    assert ds_backend["tier"] == "local"


@pytest.mark.unit
def test_resolve_delegation_backend_local_ds_v4_flash_ends_in_chat_completions() -> (
    None
):
    """End-to-end via the public resolve_delegation_backend entrypoint: the
    resolved endpoint_ref is the COMPLETE chat-completions path, never the bare
    /v1 base (the named silent-failure class this ticket guards against)."""
    store = _MockStore(
        {
            BIFROST_OVERLAY_STORE_KEY: _overlay_yaml(
                [
                    {
                        "backend_id": "local-ds-v4-flash",
                        "endpoint_url": _STABILITY_TEST_ENDPOINT,
                    }
                ]
            )
        }
    )

    resolved = resolve_delegation_backend(
        "code_generation",
        backend_id="local-ds-v4-flash",
        config_path=_BIFROST_CONFIG_PATH,
        store=store,
    )

    assert resolved.backend_id == "local-ds-v4-flash"
    assert resolved.endpoint_ref == _STABILITY_TEST_ENDPOINT
    assert resolved.endpoint_ref.endswith("/v1/chat/completions")
    assert resolved.model_id == "deepseek-v4-flash"
    assert resolved.tier == "local"
    assert resolved.secret_ref is None
    assert resolved.api_key_env is None


@pytest.mark.unit
def test_resolve_delegation_backend_rejects_bare_v1_base_class_of_failure() -> None:
    """A bare-base URL (no /chat/completions path) is carried verbatim by the
    resolver — never silently constructed into a complete path — so a
    misconfigured overlay is visibly wrong rather than a silent failure."""
    bare_base = "http://stickybeatz-studio:8101/v1"
    store = _MockStore(
        {
            BIFROST_OVERLAY_STORE_KEY: _overlay_yaml(
                [{"backend_id": "local-ds-v4-flash", "endpoint_url": bare_base}]
            )
        }
    )

    resolved = resolve_delegation_backend(
        "code_generation",
        backend_id="local-ds-v4-flash",
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
                        "backend_id": "local-ds-v4-flash-not-committed",
                        "endpoint_url": _STABILITY_TEST_ENDPOINT,
                        "model_name": "deepseek-v4-flash",
                    }
                ]
            )
        }
    )

    backends = load_bifrost_backends(config_path=_BIFROST_CONFIG_PATH, store=store)
    ids = {b["backend_id"] for b in backends}
    assert "local-ds-v4-flash-not-committed" not in ids, (
        "_merge_overlay must silently drop an overlay-only backend_id that has "
        "no matching committed entry — this is why local-ds-v4-flash MUST be "
        "committed in bifrost_delegation.yaml, not overlay-only (OMN-15155)."
    )

    with pytest.raises(RuntimeError, match="local-ds-v4-flash-not-committed"):
        resolve_delegation_backend(
            "code_generation",
            backend_id="local-ds-v4-flash-not-committed",
            config_path=_BIFROST_CONFIG_PATH,
            store=store,
        )
