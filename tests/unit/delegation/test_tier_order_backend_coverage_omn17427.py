# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declared ladders and public Gateway coverage for the C14 row2 probe."""

from __future__ import annotations

import pytest

from omnimarket.inference.task_class_authority import (
    TaskClassSelectionError,
    load_task_class_authority,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
    handler_delegation_routing as routing,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_routing_tier import (
    ModelRoutingTier,
)
from tests.unit.delegation.test_tier_endpoint_completeness_omn16811 import (
    _TASK_CONTRACT_PATH,
    _routing_config,
    _yaml_mapping,
)

pytestmark = pytest.mark.unit


def _candidates(
    task_class: str,
    tier: ModelRoutingTier,
    contract: dict[str, object],
) -> frozenset[str]:
    """Use the reducer's explicit override rule alongside capability matches."""
    model_ref = routing._get_contract_model_ref(task_class, contract=contract)
    explicit = routing._is_explicit_task_model_override(task_class, contract)
    return frozenset(
        model.backend_ref
        for model in tier.models
        if task_class in model.use_for or (explicit and model.id == model_ref)
    )


def test_every_declared_tier_order_tier_serves_its_class() -> None:
    """Protect C14 row2 on the customer machine from unserved declared rungs."""
    config = _routing_config()
    contract = _yaml_mapping(_TASK_CONTRACT_PATH)
    task_classes = contract["task_classes"]
    assert isinstance(task_classes, dict)
    tiers = {tier.name: tier for tier in config.tiers}
    failures: dict[tuple[str, str], str] = {}
    checked = 0
    for task_class in task_classes:
        entry = routing._task_class_entry(contract, task_class)
        assert entry is not None
        policy = entry.get("escalation_policy")
        if not isinstance(policy, dict) or "tier_order" not in policy:
            continue
        order = policy["tier_order"]
        assert isinstance(order, list)
        checked += 1
        for tier_name in order:
            tier = tiers.get(tier_name)
            if tier is None:
                failures[task_class, tier_name] = "unknown tier"
            elif not _candidates(task_class, tier, contract):
                failures[task_class, tier_name] = "tier_serves_class_with_no_backend"
    assert checked > 0, "no class declared a tier_order; coverage was not checked"
    assert failures == {}, f"unserved class/tier pairs in declared ladders: {failures}"


def test_every_public_class_ladder_starts_local_and_is_served_at_every_tier() -> None:
    """Walk public ladders exactly as the C14 row2 customer-machine probe does."""
    config = _routing_config()
    contract = _yaml_mapping(_TASK_CONTRACT_PATH)
    public = load_task_class_authority().public_task_classes
    assert public, "the public Gateway projection must not be empty"
    tiers = {tier.name: tier for tier in config.tiers}
    failures: dict[tuple[str, str], str] = {}
    for task_class in sorted(public):
        entry = routing._task_class_entry(contract, task_class)
        assert entry is not None
        policy = entry.get("escalation_policy")
        declared = policy.get("tier_order") if isinstance(policy, dict) else None
        order = declared or [
            tier.name for tier in routing._tier_order_from_contract(config, entry)
        ]
        assert isinstance(order, list)
        assert order, f"{task_class} public ladder must not be empty"
        assert order[0] == "local", (
            f"{task_class} public ladder must start local: {order}"
        )
        for tier_name in order:
            tier = tiers.get(tier_name)
            if tier is None:
                failures[task_class, tier_name] = "unknown tier"
            elif not _candidates(task_class, tier, contract):
                failures[task_class, tier_name] = "tier_serves_class_with_no_backend"
    assert failures == {}, f"unserved public class/tier pairs: {failures}"


def test_a_class_unavailable_for_delegation_is_internal_and_declares_no_tier_order() -> (
    None
):
    """Keep unavailable classes out of the C14 row2 public customer-machine walk."""
    contract = _yaml_mapping(_TASK_CONTRACT_PATH)
    task_classes = contract["task_classes"]
    assert isinstance(task_classes, dict)
    unavailable: set[str] = set()
    for task_class in task_classes:
        entry = routing._task_class_entry(contract, task_class)
        assert entry is not None
        if "routing_availability" not in entry:
            continue
        unavailable.add(task_class)
        assert entry["gateway_exposure"] == "internal", task_class
        policy = entry.get("escalation_policy", {})
        assert isinstance(policy, dict)
        assert "tier_order" not in policy, task_class
    assert {"code_review", "agent_delegation"} <= unavailable


def test_withheld_code_review_prompt_is_still_refused_on_auto_selection() -> None:
    """Protect C14 row2 on the customer machine from routing a withheld prompt."""
    authority = load_task_class_authority()
    with pytest.raises(TaskClassSelectionError) as refused:
        authority.resolve_task_type(
            "Review this diff for bugs: the patch changes the authentication check.",
            explicit=None,
        )
    assert str(refused.value) == authority.unroutable_refusal("code_review")
