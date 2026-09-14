# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The PROSE classes must try the FREE rung before any metered one (OMN-13640).

Measured RED-before (2026-09-14, against ``omnimarket@c2ddf6a9``): the hourly
Linear sweep delegates with ``--task-type document``. ``document``,
``documentation`` and ``summarization`` each declared::

    escalation_policy:
      max_escalations: 1
      tier_order: [local, cheap_cloud]

``cheap_frontier`` — the only tier whose declared ``cost.cost_type`` is
``free_local`` — appeared in NO prose class's ``tier_order``, so the free rung
was never attempted for prose. Every prose escalation therefore landed on the
metered ``cheap_cloud`` tier and was answered by ``gemini-2.5-flash-lite``
(backend ``cloud-gemini-flash``): 4 of 4 accepted drafts, $0.001809 mean, with
the free rungs contributing nothing.

Three properties are asserted here, deliberately over BEHAVIOUR (the real
``_select_model_for_task`` / ``_tier_order_from_contract``) rather than over
the file text, so a future edit that satisfies the letter and not the routing
still fails:

1. ``test_prose_classes_resolve_a_model_on_the_free_frontier_tier`` — each
   prose class actually RESOLVES a model on ``cheap_frontier`` with
   ``contract_model_ref=None``, so the ``default_task_model_ref`` implicit pin
   cannot manufacture a false green (the OMN-15630 AC1 discipline).
2. ``test_free_tier_precedes_every_metered_tier_for_prose_classes`` — the free
   rung is ORDERED ahead of every metered rung in the class's own closed
   ``tier_order`` (OMN-14225 free-before-paid), not merely present in it.
3. ``test_every_declared_class_can_reach_the_last_entry_of_its_tier_order`` —
   the general form of the trap this ticket walked into.
   ``max_escalations`` bounds TRANSITIONS, not tiers visited: the gate is
   ``workflow.escalation_count < max_escalation_attempts``
   (``node_delegation_orchestrator/handlers/handler_delegation_workflow.py``),
   so reaching the Nth entry of ``tier_order`` costs N-1 transitions. A
   3-entry ``tier_order`` under ``max_escalations: 1`` leaves its last entry
   UNREACHABLE and the whole declaration decorative. Asserted for EVERY
   declared class, not only this ticket's three, so any future class that
   grows a rung without raising the bound is caught by name.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from omnibase_core.enums.enum_tier_cost_type import EnumTierCostType

from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
    handler_delegation_routing as routing,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_delegation_config import (
    ModelDelegationConfig,
    parse_delegation_config_yaml,
)

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_ROUTING_TIERS_PATH = _PROJECT_ROOT / "src/omnimarket/configs/routing_tiers.yaml"
_TASK_CONTRACT_PATH = (
    _PROJECT_ROOT / "src/omnimarket/configs/task_class_contracts.v1.yaml"
)

# The three PROSE natural-language classes the hourly Linear sweep and the
# dashboard prose surfaces route through. Named literally so a rename has to
# touch this test rather than silently drop a class out of coverage.
_PROSE_TASK_CLASSES: tuple[str, ...] = ("document", "documentation", "summarization")

_FREE_TIER_NAME = "cheap_frontier"


def _routing_config() -> ModelDelegationConfig:
    return parse_delegation_config_yaml(_ROUTING_TIERS_PATH.read_text())


def _task_contract() -> dict[str, object]:
    raw = yaml.safe_load(_TASK_CONTRACT_PATH.read_text())
    assert isinstance(raw, dict), f"{_TASK_CONTRACT_PATH} must contain a YAML mapping"
    return raw


def _declared_task_classes() -> dict[str, dict[str, object]]:
    task_classes = _task_contract().get("task_classes")
    assert isinstance(task_classes, dict)
    return {k: v for k, v in task_classes.items() if isinstance(v, dict)}


def _synthetic_available_backends(
    config: ModelDelegationConfig,
) -> dict[str, routing.BifrostBackendRef]:
    """Deterministic endpoint/secret availability for a structural test.

    Mirrors ``test_routing_completeness_omn15630._synthetic_available_backends``
    so this proves ``use_for`` coverage, never live provider/network/credential
    state.
    """
    backends: dict[str, routing.BifrostBackendRef] = {}
    for tier in config.tiers:
        for model in tier.models:
            backends.setdefault(
                model.backend_ref,
                routing.BifrostBackendRef(
                    endpoint_url=(
                        f"https://{model.backend_ref}.contract.test/v1/chat/completions"
                    ),
                    model_name=model.id,
                    timeout_ms=30_000,
                    max_tokens=4096,
                ),
            )
    return backends


def _is_free(tier: object) -> bool:
    cost = getattr(tier, "cost", None)
    if cost is None:
        return getattr(tier, "cost_per_1k_tokens", 0.0) == 0.0
    return cost.cost_type is EnumTierCostType.FREE_LOCAL


def _is_metered(tier: object) -> bool:
    cost = getattr(tier, "cost", None)
    if cost is None:
        return getattr(tier, "cost_per_1k_tokens", 0.0) > 0.0
    return cost.cost_type is not EnumTierCostType.FREE_LOCAL


@pytest.mark.unit
def test_free_frontier_tier_is_declared_free() -> None:
    """Positive control for the two tests below.

    Both of them are vacuous if ``cheap_frontier`` stops being the free tier,
    so the premise is asserted directly rather than assumed: a zero finding
    from a predicate that can never be true is not evidence.
    """
    config = _routing_config()
    by_name = {tier.name: tier for tier in config.tiers}
    assert _FREE_TIER_NAME in by_name, (
        f"{_FREE_TIER_NAME} is not declared in routing_tiers.yaml"
    )
    assert _is_free(by_name[_FREE_TIER_NAME]), (
        f"{_FREE_TIER_NAME} no longer declares cost_type free_local"
    )
    assert any(_is_metered(tier) for tier in config.tiers), (
        "no metered tier declared — the free-before-paid ordering test would be vacuous"
    )


@pytest.mark.unit
def test_prose_classes_resolve_a_model_on_the_free_frontier_tier() -> None:
    """Each prose class must RESOLVE a model on the free tier, unpinned.

    Evaluated through the real ``_select_model_for_task`` with
    ``contract_model_ref=None``: ``use_for`` containing the task_type is the
    hard filter, and the implicit ``default_task_model_ref`` pin is excluded so
    it cannot mask an absent capability.
    """
    config = _routing_config()
    backends = _synthetic_available_backends(config)
    by_name = {tier.name: tier for tier in config.tiers}
    free_tier = by_name[_FREE_TIER_NAME]

    unresolved = [
        task_type
        for task_type in _PROSE_TASK_CLASSES
        if routing._select_model_for_task(
            free_tier.models,
            task_type,
            0,
            backends,
            contract_model_ref=None,
        )
        is None
    ]
    assert unresolved == [], (
        f"prose classes with no model on the free {_FREE_TIER_NAME} tier: "
        f"{unresolved} — the genuinely-free rung is never attempted for them"
    )


@pytest.mark.unit
def test_free_cloud_tier_precedes_every_metered_tier_for_prose_classes() -> None:
    """Free-before-paid, asserted on the class's own closed tier_order.

    Scoped to the free CLOUD rung by name. A looser "some free tier comes
    first" predicate is satisfied vacuously by ``local``, which is free and
    always first — it would have reported GREEN on the exact configuration
    this ticket exists to fix. The question is whether the class tries the
    free CLOUD rung before it starts spending, so that is what is asserted.

    ``_tier_order_from_contract`` is the authority on what actually routes, so
    the ordering is read back through it rather than off the raw YAML list.
    """
    config = _routing_config()
    contract = _task_contract()
    violations: dict[str, str] = {}

    for task_type in _PROSE_TASK_CLASSES:
        entry = routing._task_class_entry(contract, task_type)
        assert entry is not None, f"{task_type} is not a declared task class"
        tiers = routing._tier_order_from_contract(config, entry)
        order = [tier.name for tier in tiers]

        if _FREE_TIER_NAME not in order:
            violations[task_type] = (
                f"tier_order={order} never reaches the free {_FREE_TIER_NAME} rung"
            )
            continue
        free_index = order.index(_FREE_TIER_NAME)
        metered_positions = [i for i, tier in enumerate(tiers) if _is_metered(tier)]
        if metered_positions and free_index > min(metered_positions):
            violations[task_type] = (
                f"tier_order={order} reaches metered {order[min(metered_positions)]} "
                f"at index {min(metered_positions)} before free {_FREE_TIER_NAME} "
                f"at index {free_index}"
            )

    assert violations == {}, (
        f"prose classes that spend before trying the free cloud rung: {violations}"
    )


@pytest.mark.unit
def test_every_declared_class_can_reach_the_last_entry_of_its_tier_order() -> None:
    """``max_escalations`` must be large enough to REACH the declared ceiling.

    The gate is on TRANSITIONS (``escalation_count < max_escalation_attempts``),
    so an N-entry ``tier_order`` needs ``max_escalations >= N - 1``. Anything
    lower makes the tail of the declaration unreachable — a rung that reads as
    policy and routes as nothing.

    Asserted over every declared class so a future class that grows a rung
    without raising its bound fails here by name, rather than silently
    shipping a decorative ceiling.
    """
    unreachable: dict[str, str] = {}

    for task_type, entry in _declared_task_classes().items():
        escalation = entry.get("escalation_policy")
        if not isinstance(escalation, dict):
            continue
        tier_order = escalation.get("tier_order")
        if not isinstance(tier_order, list) or not tier_order:
            continue
        max_escalations = escalation.get("max_escalations")
        assert isinstance(max_escalations, int), (
            f"{task_type} declares a tier_order but no integer max_escalations"
        )
        required = len(tier_order) - 1
        if max_escalations < required:
            unreachable[task_type] = (
                f"tier_order={tier_order} needs max_escalations>={required}, "
                f"declared {max_escalations} — entries beyond index "
                f"{max_escalations} are unreachable"
            )

    assert unreachable == {}, (
        f"task classes whose declared ceiling cannot be reached: {unreachable}"
    )
