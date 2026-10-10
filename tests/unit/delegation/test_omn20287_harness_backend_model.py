# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Harness backend validation with forged declarations (OMN-20287)."""

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


@pytest.mark.unit
def test_valid_harness_declaration_parses() -> None:
    backend = ModelDelegationBackendConfig.model_validate(_VALID_HARNESS)
    assert backend.kind is EnumDelegationBackendKind.HARNESS
    assert backend.harness is EnumDelegationHarness.CLAUDE
    assert backend.surface is EnumDelegationBackendSurface.INTERNAL
    assert backend.tenant_scope is EnumDelegationBackendTenantScope.HOUSE
    assert backend.model_name == "claude-sonnet-5-5"
    assert backend.endpoint_url is None
    assert backend.endpoint_url_env is None
    assert backend.resolved_secret_ref is None


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
@pytest.mark.unit
def test_forged_harness_declaration_is_refused(
    override: dict[str, Any], reason: str
) -> None:
    with pytest.raises(ValidationError, match=reason):
        ModelDelegationBackendConfig.model_validate({**_VALID_HARNESS, **override})


@pytest.mark.unit
def test_codex_harness_needs_no_model_pin() -> None:
    backend = ModelDelegationBackendConfig.model_validate(
        {**_VALID_HARNESS, "harness": "codex", "provider": "openai", "model_name": None}
    )
    assert backend.model_name is None


@pytest.mark.unit
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


@pytest.mark.unit
def test_endpoint_backends_default_to_any_surface_and_tenant() -> None:
    backend = ModelDelegationBackendConfig.model_validate(
        {"backend_id": "cloud-x", "provider": "openai", "tier": "cheap_cloud"}
    )
    assert backend.kind is EnumDelegationBackendKind.ENDPOINT
    assert backend.harness is None
    assert backend.surface is EnumDelegationBackendSurface.ANY
    assert backend.tenant_scope is EnumDelegationBackendTenantScope.ANY


@pytest.mark.unit
def test_overlay_cannot_give_a_harness_backend_an_endpoint(tmp_path: Path) -> None:
    contract = tmp_path / "bifrost_delegation.yaml"
    contract.write_text(
        yaml.safe_dump(
            {
                "config_version": "1.0.0",
                "schema_version": "bifrost_delegation.v1",
                "backends": [_VALID_HARNESS],
                "routing_rules": [
                    {
                        "rule_id": "00000000-0000-4000-8000-000000000001",
                        "task_class": "document",
                        "task_class_contract_version": "1.0.0",
                        "backend_policy_version": "1.0.0",
                        "backend_ids": ["harness-x"],
                        "fallback_policy": {"action": "return_error"},
                        "shadow_policy_id": "00000000-0000-4000-8000-000000000002",
                    }
                ],
            }
        )
    )
    baseline = load_bifrost_delegation_config(config_path=contract, overlay_path=None)
    assert baseline.backends[0].endpoint_url is None
    overlay = tmp_path / "bifrost_overrides.yaml"
    overlay.write_text(
        yaml.safe_dump(
            {
                "backends": [
                    {
                        "backend_id": "harness-x",
                        "endpoint_url": "https://example.invalid/v1/chat/completions",
                    }
                ]
            }
        )
    )
    with pytest.raises((ValidationError, ValueError), match="no endpoint_url"):
        load_bifrost_delegation_config(config_path=contract, overlay_path=overlay)
