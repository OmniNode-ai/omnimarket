# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-12890 — bifrost Gemini model_name repoint: gemini-2.0-flash → gemini-2.5-flash-lite.

The AI Studio free-tier quota for gemini-2.0-flash was exhausted (HTTP 429).
gemini-2.5-flash-lite is quota-available on the same GEMINI_API_KEY. This test pins
the bifrost contract to the quota-available model name so quota regression is
detectable without a live probe.

The escalation path for node_generation_consumer is:
  local tier (Qwen3.6-35B-A3B / local-coder)
  → cheap_cloud tier (cloud-gemini-flash backend → gemini-2.5-flash-lite)
  → frontier_api tier (cloud-sonnet)

These tests verify that the committed bifrost contract carries gemini-2.5-flash-lite
(not gemini-2.0-flash) and that the node_generation_consumer contract.yaml explicitly
documents the cloud escalation backend reference.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_COMMITTED_BIFROST = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "omnimarket"
    / "configs"
    / "bifrost_delegation.yaml"
)

_GENERATION_CONTRACT = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_generation_consumer"
    / "contract.yaml"
)

_QUOTA_ZERO_MODEL = "gemini-2.0-flash"
_QUOTA_AVAILABLE_MODEL = "gemini-2.5-flash-lite"
_CLOUD_GEMINI_BACKEND_ID = "cloud-gemini-flash"


def _load_bifrost_backends() -> dict[str, dict]:  # type: ignore[type-arg]
    data = yaml.safe_load(_COMMITTED_BIFROST.read_text(encoding="utf-8"))
    return {b["backend_id"]: b for b in data["backends"]}


# ---------------------------------------------------------------------------
# Contract-level pin: gemini backend uses the quota-available model name.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cloud_gemini_flash_uses_quota_available_model() -> None:
    """OMN-12890: cloud-gemini-flash backend must declare gemini-2.5-flash-lite.

    gemini-2.0-flash is quota-zero on the AI Studio free tier (HTTP 429).
    gemini-2.5-flash-lite is quota-available on the same GEMINI_API_KEY.
    """
    backends = _load_bifrost_backends()
    assert _CLOUD_GEMINI_BACKEND_ID in backends, (
        f"cloud-gemini-flash backend missing from {_COMMITTED_BIFROST.name}"
    )
    actual = backends[_CLOUD_GEMINI_BACKEND_ID]["model_name"]
    assert actual == _QUOTA_AVAILABLE_MODEL, (
        f"cloud-gemini-flash model_name is '{actual}'; expected '{_QUOTA_AVAILABLE_MODEL}'. "
        f"'{_QUOTA_ZERO_MODEL}' is quota-exhausted (HTTP 429) — do not revert (OMN-12890)."
    )


@pytest.mark.unit
def test_cloud_gemini_flash_not_quota_zero_model() -> None:
    """OMN-12890: cloud-gemini-flash must NOT declare the quota-zero model.

    Regression guard: a revert to gemini-2.0-flash would silently break the
    cheap_cloud escalation leg with HTTP 429 rather than a schema error.
    """
    backends = _load_bifrost_backends()
    actual = backends[_CLOUD_GEMINI_BACKEND_ID]["model_name"]
    assert actual != _QUOTA_ZERO_MODEL, (
        f"cloud-gemini-flash reverted to quota-zero model '{_QUOTA_ZERO_MODEL}'. "
        "This will cause HTTP 429 on the cheap_cloud escalation leg (OMN-12890)."
    )


@pytest.mark.unit
def test_vertex_gemini_backend_also_uses_quota_available_model() -> None:
    """OMN-12890 + OMN-12971: both Gemini backends use gemini-2.5-flash-lite."""
    backends = _load_bifrost_backends()
    assert "cloud-vertex-gemini" in backends, "cloud-vertex-gemini backend missing"
    vertex_model = backends["cloud-vertex-gemini"]["model_name"]
    assert vertex_model == _QUOTA_AVAILABLE_MODEL, (
        f"cloud-vertex-gemini model_name is '{vertex_model}'; "
        f"expected '{_QUOTA_AVAILABLE_MODEL}'"
    )


# ---------------------------------------------------------------------------
# node_generation_consumer contract.yaml documents the cloud escalation path.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_generation_contract_documents_cloud_escalation_backend() -> None:
    """OMN-12890: node_generation_consumer contract.yaml must cite the cloud escalation backend.

    The contract comment block must mention 'cloud-gemini-flash' so the escalation
    path is contract-documented (not just an implicit routing-authority lookup).
    """
    raw = _GENERATION_CONTRACT.read_text(encoding="utf-8")
    assert "cloud-gemini-flash" in raw, (
        "node_generation_consumer/contract.yaml does not cite the cloud escalation "
        "backend 'cloud-gemini-flash'. Add an OMN-12890 comment documenting the "
        "cheap_cloud escalation path (OMN-12890 requirement)."
    )


@pytest.mark.unit
def test_generation_contract_documents_quota_available_model() -> None:
    """OMN-12890: node_generation_consumer contract.yaml must cite gemini-2.5-flash-lite."""
    raw = _GENERATION_CONTRACT.read_text(encoding="utf-8")
    assert _QUOTA_AVAILABLE_MODEL in raw, (
        f"node_generation_consumer/contract.yaml does not cite '{_QUOTA_AVAILABLE_MODEL}'. "
        "Add an OMN-12890 comment documenting the cloud escalation model (OMN-12890)."
    )
