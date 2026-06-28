# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-13233 — ceiling-tier backends must resolve auth via secret_ref only.

The Anthropic ceiling tier (cloud-sonnet, cloud-haiku) must follow the same
per-tenant contract+store resolution path as every other tier. Hardcoding
``api_key_env: ANTHROPIC_API_KEY`` is the legacy env-var fallback that makes
the ceiling tier special-cased. After OMN-13233:

  - cloud-sonnet and cloud-haiku carry ``secret_ref: llm.anthropic.api_key``
    (the per-tenant store path)
  - neither backend declares ``api_key_env``
  - ``ModelDelegationBackendConfig.resolved_secret_ref`` returns the
    ``secret_ref`` value for both, NOT the env-var fallback
  - ``git grep ANTHROPIC_API_KEY`` over src/ returns zero non-test hits

These tests FAIL before the fix (bifrost_delegation.yaml still has
``api_key_env: ANTHROPIC_API_KEY`` on both backends) and PASS after.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.models.delegation.wire.model_bifrost_delegation_config import (
    ModelDelegationBackendConfig,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_BIFROST_CONFIG = (
    _REPO_ROOT / "src" / "omnimarket" / "configs" / "bifrost_delegation.yaml"
)

# Ceiling-tier backends that must not carry api_key_env after OMN-13233.
_CEILING_BACKEND_IDS: tuple[str, ...] = ("cloud-sonnet", "cloud-haiku")
_EXPECTED_SECRET_REF = "llm.anthropic.api_key"


def _load_raw_backends() -> dict[str, dict]:
    """Return a dict of backend_id → raw YAML mapping from the committed config."""
    data = yaml.safe_load(_BIFROST_CONFIG.read_text(encoding="utf-8"))
    return {b["backend_id"]: b for b in data["backends"]}


# ---------------------------------------------------------------------------
# 1. api_key_env must not appear on ceiling backends (FAILS before fix)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("backend_id", _CEILING_BACKEND_IDS)
def test_ceiling_backend_has_no_api_key_env(backend_id: str) -> None:
    """OMN-13233: ceiling backends must NOT declare api_key_env.

    The api_key_env field is the legacy env-var fallback.  The ceiling tier
    resolves auth through secret_ref (per-tenant store path) identical to
    cheap_cloud and every other tier.  A committed api_key_env on the ceiling
    backend is the special-case being removed.

    This test FAILS before the fix.
    """
    backends = _load_raw_backends()
    assert backend_id in backends, (
        f"Backend {backend_id!r} not found in committed bifrost config"
    )
    backend = backends[backend_id]
    assert "api_key_env" not in backend, (
        f"OMN-13233: {backend_id!r} must not declare api_key_env "
        f"(found {backend.get('api_key_env')!r}). "
        "Remove api_key_env and rely on secret_ref for per-tenant auth resolution."
    )


# ---------------------------------------------------------------------------
# 2. secret_ref must resolve to the correct per-tenant logical ref
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("backend_id", _CEILING_BACKEND_IDS)
def test_ceiling_backend_secret_ref_is_per_tenant_path(backend_id: str) -> None:
    """OMN-13233: ceiling backends declare secret_ref for per-tenant store resolution."""
    backends = _load_raw_backends()
    backend = backends[backend_id]
    assert backend.get("secret_ref") == _EXPECTED_SECRET_REF, (
        f"OMN-13233: {backend_id!r} must declare "
        f"secret_ref: {_EXPECTED_SECRET_REF!r} "
        f"(got {backend.get('secret_ref')!r})"
    )


# ---------------------------------------------------------------------------
# 3. ModelDelegationBackendConfig.resolved_secret_ref returns secret_ref,
#    not the api_key_env fallback (exercises the model's property)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("backend_id", _CEILING_BACKEND_IDS)
def test_ceiling_backend_model_resolved_secret_ref_uses_secret_ref(
    backend_id: str,
) -> None:
    """OMN-13233: resolved_secret_ref returns the secret_ref value, not env fallback.

    After removal of api_key_env the resolved_secret_ref property must return
    the canonical ``llm.anthropic.api_key`` logical ref from secret_ref.  This
    confirms the Pydantic model reflects the cleaned YAML state.
    """
    backends = _load_raw_backends()
    raw = backends[backend_id]

    # Construct the model from the committed raw YAML mapping.
    config = ModelDelegationBackendConfig(**raw)

    assert config.api_key_env is None, (
        f"OMN-13233: {backend_id!r} api_key_env must be None after fix "
        f"(got {config.api_key_env!r})"
    )
    assert config.resolved_secret_ref == _EXPECTED_SECRET_REF, (
        f"OMN-13233: {backend_id!r} resolved_secret_ref must be "
        f"{_EXPECTED_SECRET_REF!r} (got {config.resolved_secret_ref!r})"
    )


# ---------------------------------------------------------------------------
# 4. git grep gate — no ANTHROPIC_API_KEY literal in src/ (non-test)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_no_anthropic_api_key_literal_in_src() -> None:
    """OMN-13233: 'ANTHROPIC_API_KEY' must not appear in any src/ file.

    The DoD for OMN-13233 states:
      ``git grep ANTHROPIC_API_KEY`` over omnimarket src returns zero non-test hits.

    This test scans src/ directly (no git required) so it passes in CI
    without a full git history.  Test files under tests/ are excluded.
    """
    src_root = _REPO_ROOT / "src"
    violations: list[str] = []
    for path in src_root.rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="replace")
        if "ANTHROPIC_API_KEY" in text:
            violations.append(str(path.relative_to(_REPO_ROOT)))
    for path in src_root.rglob("*.yaml"):
        text = path.read_text(encoding="utf-8", errors="replace")
        if "ANTHROPIC_API_KEY" in text:
            violations.append(str(path.relative_to(_REPO_ROOT)))

    assert not violations, (
        "OMN-13233: ANTHROPIC_API_KEY must not appear in src/ after fix.\n"
        "Found in:\n" + "\n".join(f"  {v}" for v in violations)
    )
