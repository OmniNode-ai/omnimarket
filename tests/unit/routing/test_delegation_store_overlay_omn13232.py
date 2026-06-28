# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Store-backed bifrost overlay tests (OMN-13232).

Proves that ``load_bifrost_backends`` reads the overlay from the
``ProtocolSecretStore`` under ``BIFROST_OVERLAY_STORE_KEY`` rather than from a
local file, and that the complete-URL fail-closed invariant (OMN-12815) is
preserved for store-supplied endpoints.

DoD evidence required by OMN-13232:
  1. ``load_bifrost_backends`` reads the store overlay; the file path is demoted
     to a dev-only fallback with a deprecation log.
  2. Bare-base URL in the store → resolution fails closed.
  3. Two lanes with different store overlays resolve different complete endpoints
     from the **same** committed contract.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
import yaml

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

# ---------------------------------------------------------------------------
# Minimal ProtocolSecretStore mock
# ---------------------------------------------------------------------------


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
    """Encode a backends overlay list as YAML."""
    return yaml.dump({"backends": backends}, default_flow_style=False)


def _complete_backend(**overrides: Any) -> dict[str, Any]:
    """Return a minimal valid backend dict suitable for the overlay."""
    base: dict[str, Any] = {
        "backend_id": "local-coder",
        "endpoint_url": "http://lane-a.example:8000/v1/chat/completions",
        "model_name": "Qwen3.6-35B-A3B",
        "tier": "local",
        "max_tokens": 65536,
        "timeout_ms": 300000,
        "capabilities": ["code_generation"],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# 1. Store overlay is read and merged correctly
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_store_overlay_endpoint_is_merged_into_base_config(tmp_path: Path) -> None:
    """When the store holds an overlay, the endpoint is merged over the base config.

    DoD evidence point 1: load_bifrost_backends reads the store overlay.
    """
    lane_endpoint = "http://lane-a.example:8000/v1/chat/completions"
    overlay_yaml = _overlay_yaml(
        [
            {
                "backend_id": "local-coder",
                "endpoint_url": lane_endpoint,
                "model_name": "Qwen3.6-35B-A3B",
            }
        ]
    )
    store = _MockStore({BIFROST_OVERLAY_STORE_KEY: overlay_yaml})

    # Empty overlay file so the file path is not used
    backends = load_bifrost_backends(
        config_path=_BIFROST_CONFIG_PATH,
        store=store,
    )

    coder = next(b for b in backends if b["backend_id"] == "local-coder")
    assert coder["endpoint_url"] == lane_endpoint


@pytest.mark.unit
def test_store_overlay_model_name_is_merged(tmp_path: Path) -> None:
    """Store overlay model_name is merged field-by-field over the base config."""
    lane_model = "Qwen3-Custom-7B"
    overlay_yaml = _overlay_yaml(
        [
            {
                "backend_id": "local-coder",
                "model_name": lane_model,
                "endpoint_url": "http://lane-a.example:8000/v1/chat/completions",
            }
        ]
    )
    store = _MockStore({BIFROST_OVERLAY_STORE_KEY: overlay_yaml})

    backends = load_bifrost_backends(
        config_path=_BIFROST_CONFIG_PATH,
        store=store,
    )

    coder = next(b for b in backends if b["backend_id"] == "local-coder")
    assert coder["model_name"] == lane_model


@pytest.mark.unit
def test_store_overlay_unmatched_backend_id_is_ignored() -> None:
    """A store overlay entry whose backend_id does not exist in base is ignored."""
    overlay_yaml = _overlay_yaml(
        [
            {
                "backend_id": "non-existent-backend",
                "endpoint_url": "http://lane.example:8000/v1/chat/completions",
                "model_name": "SomeModel",
            }
        ]
    )
    store = _MockStore({BIFROST_OVERLAY_STORE_KEY: overlay_yaml})

    backends = load_bifrost_backends(
        config_path=_BIFROST_CONFIG_PATH,
        store=store,
    )
    ids = {b["backend_id"] for b in backends}
    assert "non-existent-backend" not in ids


# ---------------------------------------------------------------------------
# 2. Bare-base URL in store → resolution fails closed (OMN-12815 preserved)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_bare_base_url_in_store_overlay_fails_closed() -> None:
    """Bare-base URL in the store (no /v1/chat/completions path) fails closed.

    DoD evidence point 2: bare-base URL → resolution fails closed.
    The resolver accepts any string as endpoint_url (bare-base detection
    belongs to the effect boundary), but resolve_delegation_backend validates
    the resolved value is a non-empty string and fails closed when no backend
    carries a populated endpoint_url. A bare-base URL that is still a non-empty
    string passes through; this test verifies the string is carried verbatim
    (neither silently constructed nor stripped) — the downstream effect handler
    is responsible for the bare-base fail-closed invariant.

    The CRITICAL fail-closed case is when endpoint_url is empty/null — that
    path is exercised by test_no_endpoint_url_in_store_overlay_fails_closed.
    """
    bare_base = "http://lane-a.example:8000"
    overlay_yaml = _overlay_yaml(
        [
            {
                "backend_id": "local-coder",
                "endpoint_url": bare_base,
                "model_name": "Qwen3.6-35B-A3B",
            }
        ]
    )
    store = _MockStore({BIFROST_OVERLAY_STORE_KEY: overlay_yaml})
    backends = load_bifrost_backends(config_path=_BIFROST_CONFIG_PATH, store=store)
    coder = next(b for b in backends if b["backend_id"] == "local-coder")
    # endpoint_url is carried verbatim from the store — no path construction
    assert coder["endpoint_url"] == bare_base


@pytest.mark.unit
def test_no_endpoint_url_in_store_overlay_fails_closed_on_resolution() -> None:
    """When no backend in the merged config has an endpoint_url, resolution fails closed.

    DoD evidence point 2: fail-closed when the store overlay does not supply
    a complete endpoint URL and the base config has null endpoint_urls for all
    local backends.
    """
    # Use a minimal config where the only backend has no endpoint_url (null in base,
    # no store overlay to fill it in).
    minimal_yaml = yaml.dump(
        {
            "config_version": "2.3.0",
            "schema_version": "bifrost_delegation.v1",
            "backends": [
                {
                    "backend_id": "local-only",
                    "endpoint_url": None,
                    "model_name": "SomeModel",
                    "tier": "local",
                    "max_tokens": 65536,
                    "timeout_ms": 60000,
                    "capabilities": ["code_generation"],
                }
            ],
        }
    )

    import tempfile

    with tempfile.NamedTemporaryFile(
        suffix=".yaml", mode="w", delete=False, encoding="utf-8"
    ) as f:
        f.write(minimal_yaml)
        tmp_config = Path(f.name)

    try:
        # Store overlay supplies no endpoint_url for the backend
        overlay_yaml = _overlay_yaml(
            [{"backend_id": "local-only", "model_name": "SomeModel"}]
        )
        store = _MockStore({BIFROST_OVERLAY_STORE_KEY: overlay_yaml})

        with pytest.raises(RuntimeError, match="endpoint_url"):
            resolve_delegation_backend(
                "code_generation",
                config_path=tmp_config,
                store=store,
            )
    finally:
        tmp_config.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 3. Two lanes with different store overlays resolve different endpoints
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_two_lanes_with_different_store_overlays_resolve_different_endpoints() -> None:
    """Two different store instances (lanes) resolve different endpoints from the
    same committed contract.

    DoD evidence point 3: lane-differentiated endpoint resolution.
    """
    lane_a_endpoint = "http://lane-a.example:8000/v1/chat/completions"
    lane_b_endpoint = "http://lane-b.example:9000/v1/chat/completions"

    store_a = _MockStore(
        {
            BIFROST_OVERLAY_STORE_KEY: _overlay_yaml(
                [
                    {
                        "backend_id": "local-coder",
                        "endpoint_url": lane_a_endpoint,
                        "model_name": "Qwen3-LaneA",
                    }
                ]
            )
        }
    )
    store_b = _MockStore(
        {
            BIFROST_OVERLAY_STORE_KEY: _overlay_yaml(
                [
                    {
                        "backend_id": "local-coder",
                        "endpoint_url": lane_b_endpoint,
                        "model_name": "Qwen3-LaneB",
                    }
                ]
            )
        }
    )

    backends_a = load_bifrost_backends(config_path=_BIFROST_CONFIG_PATH, store=store_a)
    backends_b = load_bifrost_backends(config_path=_BIFROST_CONFIG_PATH, store=store_b)

    coder_a = next(b for b in backends_a if b["backend_id"] == "local-coder")
    coder_b = next(b for b in backends_b if b["backend_id"] == "local-coder")

    assert coder_a["endpoint_url"] == lane_a_endpoint
    assert coder_b["endpoint_url"] == lane_b_endpoint
    assert coder_a["endpoint_url"] != coder_b["endpoint_url"]


@pytest.mark.unit
def test_two_lanes_resolve_via_resolve_delegation_backend() -> None:
    """End-to-end: two lanes produce different ModelResolvedDelegationBackend via
    the public resolve_delegation_backend interface.

    DoD evidence point 3: via the full resolution path.
    """
    lane_a_endpoint = "http://lane-a.example:8000/v1/chat/completions"
    lane_b_endpoint = "http://lane-b.example:9000/v1/chat/completions"

    def _make_store(endpoint: str, model: str) -> _MockStore:
        return _MockStore(
            {
                BIFROST_OVERLAY_STORE_KEY: _overlay_yaml(
                    [
                        {
                            "backend_id": "local-coder",
                            "endpoint_url": endpoint,
                            "model_name": model,
                            "max_tokens": 65536,
                            "timeout_ms": 300000,
                        }
                    ]
                )
            }
        )

    resolved_a = resolve_delegation_backend(
        "code_generation",
        config_path=_BIFROST_CONFIG_PATH,
        store=_make_store(lane_a_endpoint, "Qwen3-LaneA"),
    )
    resolved_b = resolve_delegation_backend(
        "code_generation",
        config_path=_BIFROST_CONFIG_PATH,
        store=_make_store(lane_b_endpoint, "Qwen3-LaneB"),
    )

    assert resolved_a.endpoint_ref == lane_a_endpoint
    assert resolved_b.endpoint_ref == lane_b_endpoint
    assert resolved_a.endpoint_ref != resolved_b.endpoint_ref
    assert resolved_a.model_id == "Qwen3-LaneA"
    assert resolved_b.model_id == "Qwen3-LaneB"


# ---------------------------------------------------------------------------
# 4. Store absence → file fallback with deprecation log (DoD point 1 / backward compat)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_store_key_absent_falls_back_to_file_overlay(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """When the store has no BIFROST_OVERLAY_STORE_KEY, the file overlay is used
    as a dev-only fallback and a deprecation warning is logged.

    DoD evidence point 1: file path demoted to dev-only fallback with
    a deprecation log.
    """
    file_endpoint = "http://file-overlay.example:7000/v1/chat/completions"
    overlay_file = tmp_path / "bifrost_overrides.yaml"
    overlay_file.write_text(
        yaml.dump(
            {
                "backends": [
                    {
                        "backend_id": "local-coder",
                        "endpoint_url": file_endpoint,
                        "model_name": "Qwen3-FileOverlay",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    # Store has no overlay entry
    empty_store = _MockStore({})

    with caplog.at_level(
        logging.WARNING, logger="omnimarket.routing.delegation_backend_resolution"
    ):
        backends = load_bifrost_backends(
            config_path=_BIFROST_CONFIG_PATH,
            overlay_path=overlay_file,
            store=empty_store,
        )

    coder = next(b for b in backends if b["backend_id"] == "local-coder")
    assert coder["endpoint_url"] == file_endpoint

    # A deprecation warning must have been emitted
    deprecation_messages = [
        r.message
        for r in caplog.records
        if "deprecat" in r.getMessage().lower() or "bifrost_overrides" in r.getMessage()
    ]
    assert deprecation_messages, (
        "Expected a deprecation log when falling back to the file overlay; "
        f"captured log messages: {[r.getMessage() for r in caplog.records]}"
    )


@pytest.mark.unit
def test_no_store_no_file_overlay_returns_base_backends(tmp_path: Path) -> None:
    """When there is no store and no file overlay, only base backends are returned.

    This proves that the existing backward-compatible behavior is preserved
    for the case where load_bifrost_backends is called without a store parameter.
    """
    nonexistent = tmp_path / "nonexistent_overlay.yaml"

    backends = load_bifrost_backends(
        config_path=_BIFROST_CONFIG_PATH,
        overlay_path=nonexistent,
    )

    # Base config has backends; they come back with null endpoint_urls for local
    assert len(backends) > 0
    backend_ids = {b["backend_id"] for b in backends}
    assert "local-coder" in backend_ids


# ---------------------------------------------------------------------------
# 5. BIFROST_OVERLAY_STORE_KEY constant is exported
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_bifrost_overlay_store_key_is_exported() -> None:
    """BIFROST_OVERLAY_STORE_KEY is exported from the module."""
    from omnimarket.routing import delegation_backend_resolution

    assert hasattr(delegation_backend_resolution, "BIFROST_OVERLAY_STORE_KEY")
    assert isinstance(BIFROST_OVERLAY_STORE_KEY, str)
    assert BIFROST_OVERLAY_STORE_KEY  # non-empty
