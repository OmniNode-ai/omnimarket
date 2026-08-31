# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Tests for the backend secret-discipline gate (OMN-12971).

Verifies the enforcement ratchet that keeps credential VALUES out of committed
routing-authority config: the live repo config passes, and synthetic configs
with a leaked credential or a missing logical ref fail.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "ci"
    / "check_backend_secret_discipline.py"
)


def _load_module() -> object:
    spec = importlib.util.spec_from_file_location(
        "check_backend_secret_discipline", _SCRIPT
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


@pytest.mark.unit
def test_live_repo_config_passes() -> None:
    """The committed routing-authority config must satisfy the discipline gate."""
    module = _load_module()
    report = module.build_report(_repo_root())
    assert report["passed"], (
        f"live config violates secret discipline: "
        f"{report['literal_credential_violations']} "
        f"{report['backend_ref_violations']} {report['errors']}"
    )


@pytest.mark.unit
def test_vertex_backend_uses_secret_ref_bearer_path() -> None:
    """The Vertex backend declares only a logical secret ref (bearer-token path).

    The Vertex OAuth bearer token is minted from ADC at the secret-store / seed
    boundary and published under ``llm.vertex.access_token``. The committed
    config carries only the logical ref NAME — never the token VALUE, never a
    credential file path, never a service-account JSON. The outbound call reuses
    the standard ``Authorization: Bearer <token>`` path shared by every cloud
    backend, so no inference-handler change is required.
    """
    import yaml

    config = _repo_root() / "src" / "omnimarket" / "configs" / "bifrost_delegation.yaml"
    data = yaml.safe_load(config.read_text(encoding="utf-8"))
    vertex = next(
        b for b in data["backends"] if b.get("backend_id") == "cloud-vertex-gemini"
    )
    assert vertex.get("secret_ref") == "llm.vertex.access_token"
    # No literal api-key env on the Vertex path (token resolves via the ref).
    assert "api_key_env" not in vertex
    # endpoint_url must be null in the repo default (overlay supplies complete URL).
    assert vertex.get("endpoint_url") is None
    assert vertex.get("endpoint_url_env") == "BIFROST_VERTEX_GEMINI_ENDPOINT_URL"


@pytest.mark.unit
def test_no_backend_declares_an_api_key_env_fallback() -> None:
    """OMN-17372: the house env-var fallback is deleted from every backend.

    ``api_key_env`` named a HOUSE environment variable, so a backend carrying
    one authenticated on OmniNode's own provider account the moment that
    variable held a value. That is how a keyless customer's delegation --
    routed to the platform-default ladder by the fail-open tenant-overlay miss
    -- executed on our credential instead of receiving an honest refusal.

    OmniNode does not offer inference and there are no keyless customers on the
    cloud, so the field is gone rather than discouraged.
    """
    import yaml

    config = _repo_root() / "src" / "omnimarket" / "configs" / "bifrost_delegation.yaml"
    data = yaml.safe_load(config.read_text(encoding="utf-8"))
    offenders = {
        backend.get("backend_id", "<unknown>"): backend["api_key_env"]
        for backend in data["backends"]
        if isinstance(backend, dict) and "api_key_env" in backend
    }
    assert not offenders, (
        f"backends still declare a house env-var fallback: {offenders}. A "
        f"backend authenticates from its managed-store secret_ref or not at "
        f"all (OMN-17372)."
    )


@pytest.mark.unit
def test_api_key_env_is_detected_when_reintroduced() -> None:
    """The gate must FIRE on a re-added field -- a green gate proves nothing.

    Deleting the field from config is undone by one line in a future PR. This
    asserts the scanner would catch that line, so the removal is enforced
    rather than merely performed.
    """
    module = _load_module()
    data = {
        "backends": [
            {
                "backend_id": "openrouter-glm-flash",
                "tier": "cheap_cloud",
                "endpoint_url": "https://openrouter.ai/api/v1/chat/completions",
                "secret_ref": "llm.openrouter.api_key",
                "api_key_env": "OPENROUTER_API_KEY",
            },
            {
                "backend_id": "cloud-gemini-flash",
                "tier": "cheap_cloud",
                "endpoint_url": "https://example.invalid/v1/chat/completions",
                "secret_ref": "llm.gemini.api_key",
            },
        ]
    }
    violations = module._scan_api_key_env("fake.yaml", data)
    assert len(violations) == 1, violations
    assert "openrouter-glm-flash" in violations[0]
    assert "OPENROUTER_API_KEY" in violations[0]
    # A backend carrying only secret_ref is clean -- the rule bans the env
    # indirection, not authenticated cloud backends.
    assert "cloud-gemini-flash" not in violations[0]


@pytest.mark.unit
def test_api_key_env_alone_no_longer_counts_as_a_logical_ref() -> None:
    """A backend whose ONLY credential surface is api_key_env is unreferenced.

    Before OMN-17372 ``api_key_env`` satisfied ``_backend_has_logical_ref``, so
    such a backend passed the discipline gate. It never was a logical ref --
    it pointed at a house env var, the opposite of store indirection. It must
    now fail as a cloud backend with no declared reference.
    """
    module = _load_module()
    violations = module._scan_bifrost_backends(
        "fake.yaml",
        {
            "backends": [
                {
                    "backend_id": "cloud-env-only",
                    "tier": "cheap_cloud",
                    "endpoint_url": "https://example.invalid/v1/chat/completions",
                    "api_key_env": "OPENROUTER_API_KEY",
                }
            ]
        },
    )
    assert any("cloud-env-only" in v for v in violations), violations


@pytest.mark.unit
def test_gemini_key_path_preserved() -> None:
    """Vertex wiring is ADDITIVE — the AI Studio key path must still exist."""
    import yaml

    config = _repo_root() / "src" / "omnimarket" / "configs" / "bifrost_delegation.yaml"
    data = yaml.safe_load(config.read_text(encoding="utf-8"))
    gemini = next(
        b for b in data["backends"] if b.get("backend_id") == "cloud-gemini-flash"
    )
    assert gemini.get("secret_ref") == "llm.gemini.api_key"


@pytest.mark.unit
def test_literal_pem_credential_detected(tmp_path: Path) -> None:
    module = _load_module()
    leaked = (
        'endpoint_url: null\n  service_account: "-----BEGIN PRIVATE KEY-----MIIabc"\n'
    )
    violations = module._scan_literal_credentials("fake.yaml", leaked)
    assert any("pem-private-key" in v for v in violations)


@pytest.mark.unit
def test_literal_api_key_detected() -> None:
    module = _load_module()
    leaked = 'api_key: "AIzaSyABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"\n'
    violations = module._scan_literal_credentials("fake.yaml", leaked)
    assert any("google-api-key" in v for v in violations)


@pytest.mark.unit
def test_cloud_backend_missing_ref_detected() -> None:
    module = _load_module()
    data = {
        "backends": [
            {
                "backend_id": "cloud-rogue",
                "tier": "cheap_cloud",
                "endpoint_url": "https://example.com/v1/chat/completions",
            }
        ]
    }
    violations = module._scan_bifrost_backends("fake.yaml", data)
    assert any("requires a logical secret reference" in v for v in violations)


@pytest.mark.unit
def test_mutually_exclusive_auth_detected() -> None:
    module = _load_module()
    data = {
        "backends": [
            {
                "backend_id": "cloud-both",
                "tier": "cheap_cloud",
                "credential_ref": "llm.vertex.adc",
                "secret_ref": "llm.gemini.api_key",
            }
        ]
    }
    violations = module._scan_bifrost_backends("fake.yaml", data)
    assert any("mutually exclusive" in v for v in violations)
