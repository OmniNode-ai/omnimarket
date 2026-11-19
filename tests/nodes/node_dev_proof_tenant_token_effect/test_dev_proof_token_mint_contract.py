# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The node's contract declares the dev-only block, the overlay example loads, and the package is registered."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_dev_proof_tenant_token_effect.overlay import (
    ModelDevProofTenantTokenOverlay,
)

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
NODE = ROOT / "src/omnimarket/nodes/node_dev_proof_tenant_token_effect"


def test_dev_proof_token_mint_contract_declares_the_dev_only_block() -> None:
    contract = yaml.safe_load((NODE / "contract.yaml").read_text(encoding="utf-8"))
    assert contract["dev_only"] == {
        "lane": "dev",
        "audience": "gateway-attach",
        "expiry_seconds": 3600,
        "tenants": ["cli-run-h051", "cli-run-h201"],
    }


def test_dev_proof_token_mint_contract_example_overlay_loads_and_holds_no_value() -> (
    None
):
    document = yaml.safe_load(
        (NODE / "overlay.example.yaml").read_text(encoding="utf-8")
    )
    overlay = ModelDevProofTenantTokenOverlay.model_validate(document)
    assert overlay.lane == "dev"
    assert sorted(t.tenant_slug for t in overlay.tenants) == [
        "cli-run-h051",
        "cli-run-h201",
    ]
    assert "BEGIN" not in (NODE / "overlay.example.yaml").read_text(encoding="utf-8")


def test_dev_proof_token_mint_contract_overlay_refuses_repeated_secret_names() -> None:
    document = yaml.safe_load(
        (NODE / "overlay.example.yaml").read_text(encoding="utf-8")
    )
    document["tenants"][1]["secret_name"] = document["tenants"][0]["secret_name"]
    with pytest.raises(ValueError, match="secret_name"):
        ModelDevProofTenantTokenOverlay.model_validate(document)


def test_dev_proof_token_mint_contract_overlay_refuses_a_store_with_no_address() -> (
    None
):
    document = yaml.safe_load(
        (NODE / "overlay.example.yaml").read_text(encoding="utf-8")
    )
    del document["store"]["address"]
    with pytest.raises(ValueError, match="address"):
        ModelDevProofTenantTokenOverlay.model_validate(document)


def test_dev_proof_token_mint_contract_package_is_registered_under_onex_nodes() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    nodes = pyproject["project"]["entry-points"]["onex.nodes"]
    assert (
        nodes["node_dev_proof_tenant_token_effect"]
        == "omnimarket.nodes.node_dev_proof_tenant_token_effect"
    )
