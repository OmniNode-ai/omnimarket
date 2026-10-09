# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Packaged config parity: private harness routing never ships (OMN-20287)."""

from pathlib import Path

import pytest
import yaml

from omnimarket.models.delegation.wire.model_bifrost_delegation_config import (
    EnumDelegationBackendSurface,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_harness_escalation_chain import (
    resolve_class_escalation_chain,
)
from omnimarket.routing.routing_tiers_path import load_delegation_routing_overlay

pytestmark = pytest.mark.unit
_CONFIGS = Path(__file__).resolve().parents[3] / "src/omnimarket/configs"


def test_packaged_configs_declare_no_harness_routing() -> None:
    bifrost = yaml.safe_load((_CONFIGS / "bifrost_delegation.yaml").read_text())
    tiers = yaml.safe_load((_CONFIGS / "routing_tiers.yaml").read_text())
    classes = yaml.safe_load((_CONFIGS / "task_class_contracts.v1.yaml").read_text())
    assert bifrost["backends"]
    assert all(backend.get("kind") != "harness" for backend in bifrost["backends"])
    assert "harness_tiers" not in tiers
    assert classes["task_classes"]
    assert all(
        "escalation_chain" not in entry for entry in classes["task_classes"].values()
    )


def test_unbound_overlay_has_no_chain_for_any_packaged_class(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DELEGATION_ROUTING_OVERLAY_PATH", raising=False)
    overlay = load_delegation_routing_overlay()
    assert overlay.harness_backends == ()
    assert overlay.harness_tiers == ()
    assert overlay.escalation_chains == ()
    classes = yaml.safe_load((_CONFIGS / "task_class_contracts.v1.yaml").read_text())
    for task_class in classes["task_classes"]:
        assert (
            resolve_class_escalation_chain(
                task_class,
                tenant_id="omninode",
                surface=EnumDelegationBackendSurface.INTERNAL,
                ladder_tier_names=frozenset({"local"}),
            )
            is None
        )
