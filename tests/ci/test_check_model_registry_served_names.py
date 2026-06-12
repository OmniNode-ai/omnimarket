# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for the Gemini registry/version hygiene gate (OMN-12972, plan P2.7)."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.ci.check_model_registry_served_names import evaluate

_GOOD_REGISTRY = """\
schema_version: "1.0.0"
model_registry_version: "1.3.0"
pricing_manifest_version: "test"
observed_at: "2026-06-11T00:00:00Z"
models:
  gemini-2.5-flash-lite:
    model_id: "gemini-2.5-flash-lite"
    provider: "google"
    endpoint_env: "GEMINI_API_URL"
    model_name: "gemini-2.5-flash-lite"
    served_model_names:
      ai_studio: "gemini-2.5-flash-lite"
      vertex: "publishers/google/models/gemini-2.5-flash-lite"
    context_window: 1048576
    pricing_per_1m_input: "0.10"
    pricing_per_1m_output: "0.40"
    cost_basis: "cloud_api_cost"
    observed_at: "2026-06-11T00:00:00Z"
    source: "provider docs"
"""

_GOOD_BIFROST = """\
config_version: "2.0.0"
schema_version: "bifrost_delegation.v1"
backends:
  - backend_id: cloud-gemini-flash
    endpoint_url: "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
    model_name: "gemini-2.5-flash-lite"
    tier: cheap_cloud
"""


def _write_repo(
    tmp_path: Path, registry_yaml: str, bifrost_yaml: str = _GOOD_BIFROST
) -> Path:
    (tmp_path / ".git").mkdir()
    reg = tmp_path / "src/omnimarket/data/model_registry/model_registry_v1.yaml"
    reg.parent.mkdir(parents=True, exist_ok=True)
    reg.write_text(registry_yaml)
    bif = tmp_path / "src/omnimarket/configs/bifrost_delegation.yaml"
    bif.parent.mkdir(parents=True, exist_ok=True)
    bif.write_text(bifrost_yaml)
    return tmp_path


@pytest.mark.unit
def test_passes_on_well_formed_per_environment_registry(tmp_path: Path) -> None:
    root = _write_repo(tmp_path, _GOOD_REGISTRY)
    result = evaluate(root)
    assert result["passed"], result["errors"]
    assert "gemini-2.5-flash-lite" in result["checked"]


@pytest.mark.unit
def test_fails_on_dead_gemini_2_0_flash(tmp_path: Path) -> None:
    bad = _GOOD_REGISTRY.replace(
        '      ai_studio: "gemini-2.5-flash-lite"',
        '      ai_studio: "gemini-2.0-flash"',
    ).replace(
        '    model_name: "gemini-2.5-flash-lite"',
        '    model_name: "gemini-2.0-flash"',
        1,
    )
    root = _write_repo(tmp_path, bad)
    result = evaluate(root)
    assert not result["passed"]
    assert any("gemini-2.0-flash" in e for e in result["errors"])


@pytest.mark.unit
def test_fails_on_bare_vertex_name(tmp_path: Path) -> None:
    bad = _GOOD_REGISTRY.replace(
        '      vertex: "publishers/google/models/gemini-2.5-flash-lite"',
        '      vertex: "gemini-2.5-flash-lite"',
    )
    root = _write_repo(tmp_path, bad)
    result = evaluate(root)
    assert not result["passed"]
    assert any("publisher-qualified" in e for e in result["errors"])


@pytest.mark.unit
def test_fails_on_publisher_qualified_ai_studio_name(tmp_path: Path) -> None:
    bad = _GOOD_REGISTRY.replace(
        '      ai_studio: "gemini-2.5-flash-lite"',
        '      ai_studio: "publishers/google/models/gemini-2.5-flash-lite"',
    )
    root = _write_repo(tmp_path, bad)
    result = evaluate(root)
    assert not result["passed"]
    assert any("AI Studio" in e for e in result["errors"])


@pytest.mark.unit
def test_fails_on_ai_studio_model_name_drift(tmp_path: Path) -> None:
    # model_name and ai_studio served name disagree.
    bad = _GOOD_REGISTRY.replace(
        '    model_name: "gemini-2.5-flash-lite"',
        '    model_name: "gemini-2.5-flash"',
        1,
    )
    # keep the bifrost backend matching the (now-drifted) model_name to isolate
    # the ai_studio-drift error from the registry↔backend-drift error.
    bif = _GOOD_BIFROST.replace(
        '    model_name: "gemini-2.5-flash-lite"',
        '    model_name: "gemini-2.5-flash"',
    )
    root = _write_repo(tmp_path, bad, bif)
    result = evaluate(root)
    assert not result["passed"]
    assert any("must equal served_model_names" in e for e in result["errors"])


@pytest.mark.unit
def test_fails_on_registry_backend_drift(tmp_path: Path) -> None:
    bif = _GOOD_BIFROST.replace(
        '    model_name: "gemini-2.5-flash-lite"',
        '    model_name: "gemini-2.0-flash"',
    )
    root = _write_repo(tmp_path, _GOOD_REGISTRY, bif)
    result = evaluate(root)
    assert not result["passed"]
    assert any("no backend declares model_name" in e for e in result["errors"])


@pytest.mark.unit
def test_real_committed_registry_passes() -> None:
    """The live committed registry + bifrost config must pass the gate."""
    repo_root = Path(__file__).resolve().parents[2]
    result = evaluate(repo_root)
    assert result["passed"], result["errors"]
    assert "gemini-2.5-flash-lite" in result["checked"]
