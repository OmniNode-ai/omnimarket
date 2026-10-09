# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20287: harness backends are declared in the bifrost contract.

Plan step 7 of the delegation canonical workflow. RULING 2026-10-05T22:27:48Z
(decision D1) amends INV-064 so Codex, GLM through Claude Code and Claude Code
may be delegation backends, internal surface and house tenant only (INV-068).
RULING 2026-10-09T00:33:15Z (OMN-20477) names the rungs. This change declares
them; no tier places them yet, so ordinary routing is unchanged.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.adapters.llm.bifrost.config_loader_bifrost_delegation import (
    load_bifrost_delegation_config,
)
from omnimarket.models.delegation.wire.model_bifrost_delegation_config import (
    EnumDelegationBackendKind,
    EnumDelegationBackendSurface,
    EnumDelegationBackendTenantScope,
    EnumDelegationHarness,
    ModelDelegationBackendConfig,
)

pytestmark = pytest.mark.unit

_CONFIGS = Path(__file__).resolve().parents[3] / "src" / "omnimarket" / "configs"
_CONTRACT = _CONFIGS / "bifrost_delegation.yaml"
_TIERS = _CONFIGS / "routing_tiers.yaml"

# The rungs RULING 2026-10-09T00:33:15Z names beyond local Qwen3.8-27B.
_EXPECTED: dict[str, tuple[EnumDelegationHarness, str | None]] = {
    "harness-codex": (EnumDelegationHarness.CODEX, None),
    "harness-claude-glm-flash": (EnumDelegationHarness.CLAUDE_GLM, "glm-5.3-flash"),
    "harness-claude-glm": (EnumDelegationHarness.CLAUDE_GLM, "glm-5.3"),
    "harness-claude-haiku": (EnumDelegationHarness.CLAUDE, "claude-haiku-5-5"),
    "harness-claude-sonnet": (EnumDelegationHarness.CLAUDE, "claude-sonnet-5-5"),
    "harness-claude-opus": (EnumDelegationHarness.CLAUDE, "claude-opus-5-5"),
}


def _harness_backends() -> dict[str, ModelDelegationBackendConfig]:
    config = load_bifrost_delegation_config(config_path=_CONTRACT, overlay_path=None)
    return {
        backend.backend_id: backend
        for backend in config.backends
        if backend.kind is EnumDelegationBackendKind.HARNESS
    }


def _backend_ids_in(node: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "backend_id" and isinstance(value, str):
                found.add(value)
            elif key == "backend_ids" and isinstance(value, list):
                found.update(str(item) for item in value)
            else:
                found |= _backend_ids_in(value)
    elif isinstance(node, list):
        for item in node:
            found |= _backend_ids_in(item)
    return found


def test_every_ruled_harness_rung_is_declared() -> None:
    backends = _harness_backends()
    assert set(backends) == set(_EXPECTED)
    for backend_id, (harness, model_name) in _EXPECTED.items():
        backend = backends[backend_id]
        assert backend.harness is harness
        assert backend.model_name == model_name


def test_harness_backends_are_internal_house_only_and_keyless() -> None:
    for backend in _harness_backends().values():
        assert backend.surface is EnumDelegationBackendSurface.INTERNAL
        assert backend.tenant_scope is EnumDelegationBackendTenantScope.HOUSE
        assert backend.endpoint_url is None
        assert backend.endpoint_url_env is None
        assert backend.resolved_secret_ref is None


def test_glm_rungs_run_inside_claude_code_not_the_parked_http_rungs() -> None:
    glm = [
        backend
        for backend in _harness_backends().values()
        if backend.model_name in {"glm-5.3-flash", "glm-5.3"}
    ]
    assert len(glm) == 2
    assert all(backend.harness is EnumDelegationHarness.CLAUDE_GLM for backend in glm)


def test_this_change_is_inert_no_tier_or_rule_places_a_harness_backend() -> None:
    tiers = yaml.safe_load(_TIERS.read_text())
    contract = yaml.safe_load(_CONTRACT.read_text())
    placed = _backend_ids_in(tiers) | _backend_ids_in(contract["routing_rules"])
    placed |= _backend_ids_in(contract.get("default_backends", {}))
    assert placed.isdisjoint(_EXPECTED)
    assert all(backend.explicit_pin_only for backend in _harness_backends().values())


_VALID_HARNESS: dict[str, Any] = {
    "backend_id": "harness-x",
    "provider": "anthropic",
    "kind": "harness",
    "harness": "claude",
    "surface": "internal",
    "tenant_scope": "house",
    "model_name": "claude-sonnet-5-5",
    "tier": "harness",
}


def test_valid_harness_declaration_parses() -> None:
    backend = ModelDelegationBackendConfig.model_validate(_VALID_HARNESS)
    assert backend.kind is EnumDelegationBackendKind.HARNESS


@pytest.mark.parametrize(
    ("override", "reason"),
    [
        ({"surface": "any"}, "surface must be internal"),
        ({"tenant_scope": "any"}, "tenant_scope must be house"),
        ({"harness": None}, "harness is not declared"),
        (
            {"endpoint_url": "https://example.invalid/v1/chat/completions"},
            "no endpoint_url",
        ),
        ({"endpoint_url_env": "SOME_URL"}, "no endpoint_url"),
        ({"secret_ref": "llm.anthropic.api_key"}, "no secret_ref"),
        ({"model_name": None}, "model_name is not declared"),
    ],
)
def test_forged_harness_declaration_is_refused(
    override: dict[str, Any], reason: str
) -> None:
    with pytest.raises(ValidationError, match=reason):
        ModelDelegationBackendConfig.model_validate({**_VALID_HARNESS, **override})


def test_codex_harness_needs_no_model_pin() -> None:
    backend = ModelDelegationBackendConfig.model_validate(
        {**_VALID_HARNESS, "harness": "codex", "provider": "openai", "model_name": None}
    )
    assert backend.model_name is None


def test_endpoint_backend_cannot_name_a_harness() -> None:
    with pytest.raises(ValidationError, match="harness is set on an endpoint backend"):
        ModelDelegationBackendConfig.model_validate(
            {
                "backend_id": "cloud-x",
                "provider": "openai",
                "harness": "codex",
                "tier": "cheap_cloud",
            }
        )


def test_endpoint_backends_default_to_any_surface_and_tenant() -> None:
    config = load_bifrost_delegation_config(config_path=_CONTRACT, overlay_path=None)
    endpoints = [
        backend
        for backend in config.backends
        if backend.kind is EnumDelegationBackendKind.ENDPOINT
    ]
    assert endpoints
    assert all(backend.harness is None for backend in endpoints)


def test_overlay_cannot_give_a_harness_backend_an_endpoint(tmp_path: Path) -> None:
    overlay = tmp_path / "bifrost_overrides.yaml"
    overlay.write_text(
        yaml.safe_dump(
            {
                "backends": [
                    {
                        "backend_id": "harness-codex",
                        "endpoint_url": "https://example.invalid/v1/chat/completions",
                    }
                ]
            }
        )
    )
    with pytest.raises((ValidationError, ValueError), match="no endpoint_url"):
        load_bifrost_delegation_config(config_path=_CONTRACT, overlay_path=overlay)
