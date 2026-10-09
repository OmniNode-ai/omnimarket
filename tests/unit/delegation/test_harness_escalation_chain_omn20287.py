# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""House harness chains and live-ladder isolation (OMN-20287).

Exercise the shipped contracts at config-resolution level only: no network I/O.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.adapters.llm.bifrost.config_loader_bifrost_delegation import (
    load_bifrost_delegation_config,
)
from omnimarket.enums import EnumHarnessRungRefusal
from omnimarket.models.delegation.model_harness_tier import ModelHarnessTier
from omnimarket.models.delegation.model_resolved_escalation_chain import (
    ModelResolvedEscalationChain,
)
from omnimarket.models.delegation.wire.model_bifrost_delegation_config import (
    EnumDelegationBackendKind,
    EnumDelegationBackendSurface,
    ModelDelegationBackendConfig,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
    handler_delegation_routing as routing,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_harness_escalation_chain import (
    resolve_class_escalation_chain,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_delegation_config import (
    parse_delegation_config_yaml,
)
from omnimarket.routing.routing_tiers_path import load_harness_tiers

_CONFIGS = Path(__file__).resolve().parents[3] / "src/omnimarket/configs"
_BIFROST = _CONFIGS / "bifrost_delegation.yaml"
_TIERS = _CONFIGS / "routing_tiers.yaml"
_CONTRACT = _CONFIGS / "task_class_contracts.v1.yaml"
_GOLDEN = {
    "summarization": (("local", None), ("harness_sonnet", "claude-sonnet-5-5")),
    "document": (
        ("local", None),
        ("harness_glm_flash", "glm-5.3-flash"),
        ("harness_glm", "glm-5.3"),
        ("harness_haiku", "claude-haiku-5-5"),
        ("harness_sonnet", "claude-sonnet-5-5"),
    ),
    "code_review": (
        ("harness_codex", None),
        ("harness_glm", "glm-5.3"),
        ("harness_sonnet", "claude-sonnet-5-5"),
    ),
    "test": (
        ("harness_codex", None),
        ("harness_haiku", "claude-haiku-5-5"),
        ("harness_sonnet", "claude-sonnet-5-5"),
    ),
    "code_generation": (
        ("harness_codex", None),
        ("local", None),
        ("harness_sonnet", "claude-sonnet-5-5"),
    ),
    "reasoning": (
        ("local", None),
        ("harness_glm", "glm-5.3"),
        ("harness_sonnet", "claude-sonnet-5-5"),
    ),
}


@pytest.fixture
def shipped_backends() -> tuple[ModelDelegationBackendConfig, ...]:
    return tuple(
        backend
        for backend in load_bifrost_delegation_config(_BIFROST).backends
        if backend.kind is EnumDelegationBackendKind.HARNESS
    )


@pytest.fixture
def shipped_tiers() -> tuple[ModelHarnessTier, ...]:
    return load_harness_tiers(_TIERS)


@pytest.fixture
def shipped_contract() -> dict[str, object]:
    loaded = yaml.safe_load(_CONTRACT.read_text())
    assert isinstance(loaded, dict)
    return loaded


@pytest.fixture
def ladder_names() -> frozenset[str]:
    return frozenset(
        tier.name for tier in parse_delegation_config_yaml(_TIERS.read_text()).tiers
    )


def _resolve(
    task_type: str,
    backends: tuple[ModelDelegationBackendConfig, ...],
    tiers: tuple[ModelHarnessTier, ...],
    contract: dict[str, object],
    ladder: frozenset[str],
) -> ModelResolvedEscalationChain:
    chain = resolve_class_escalation_chain(
        task_type,
        tenant_id="omninode",
        surface=EnumDelegationBackendSurface.INTERNAL,
        backends=backends,
        harness_tiers=tiers,
        task_class_contract=contract,
        ladder_tier_names=ladder,
    )
    assert chain is not None
    return chain


@pytest.mark.unit
@pytest.mark.parametrize("task_type", _GOLDEN)
def test_golden_chain(
    task_type: str,
    shipped_backends: tuple[ModelDelegationBackendConfig, ...],
    shipped_tiers: tuple[ModelHarnessTier, ...],
    shipped_contract: dict[str, object],
    ladder_names: frozenset[str],
) -> None:
    chain = _resolve(
        task_type, shipped_backends, shipped_tiers, shipped_contract, ladder_names
    )
    assert chain.task_type == task_type
    assert chain.escalate_on == "acceptance_check"
    assert (
        tuple((rung.tier, rung.model_name) for rung in chain.rungs)
        == _GOLDEN[task_type]
    )
    backend_ids = {tier.name: tier.backend_id for tier in shipped_tiers}
    for rung in chain.rungs:
        assert rung.kind == ("ladder" if rung.tier == "local" else "harness")
        assert rung.backend_id == backend_ids.get(rung.tier)


@pytest.mark.unit
@pytest.mark.parametrize("task_type", _GOLDEN)
def test_shipped_harness_rungs_are_pinned(
    task_type: str,
    shipped_backends: tuple[ModelDelegationBackendConfig, ...],
    shipped_tiers: tuple[ModelHarnessTier, ...],
    shipped_contract: dict[str, object],
    ladder_names: frozenset[str],
) -> None:
    assert all(backend.explicit_pin_only for backend in shipped_backends)
    chain = _resolve(
        task_type, shipped_backends, shipped_tiers, shipped_contract, ladder_names
    )
    for rung in chain.rungs:
        assert rung.refusals == (
            (EnumHarnessRungRefusal.NOT_PIN_ROUTABLE,) if rung.kind == "harness" else ()
        )
        assert rung.selectable == (rung.kind == "ladder")


@pytest.mark.unit
def test_live_endpoints_contain_no_harness_backends(
    shipped_backends: tuple[ModelDelegationBackendConfig, ...],
) -> None:
    routing._load_bifrost_endpoints.cache_clear()
    try:
        endpoints = routing._load_bifrost_endpoints()
        assert endpoints
        assert {backend.backend_id for backend in shipped_backends}.isdisjoint(
            endpoints
        )
    finally:
        routing._load_bifrost_endpoints.cache_clear()


@pytest.mark.unit
def test_live_ladder_contains_no_harness_tiers() -> None:
    config = parse_delegation_config_yaml(_TIERS.read_text())
    assert config.tiers
    assert all(not tier.name.startswith("harness_") for tier in config.tiers)


@pytest.mark.unit
def test_live_class_policies_contain_no_harness_tiers() -> None:
    classes = yaml.safe_load(_CONTRACT.read_text())["task_classes"]
    assert classes
    for task_type, entry in classes.items():
        order = entry.get("escalation_policy", {}).get("tier_order", [])
        assert all(not name.startswith("harness_") for name in order), task_type


@pytest.mark.unit
def test_harness_tiers_name_only_declared_harness_backends(
    shipped_backends: tuple[ModelDelegationBackendConfig, ...],
    shipped_tiers: tuple[ModelHarnessTier, ...],
) -> None:
    assert len(shipped_backends) == 6
    assert len(shipped_tiers) == 5
    assert {tier.backend_id for tier in shipped_tiers} == {
        backend.backend_id
        for backend in shipped_backends
        if backend.backend_id != "harness-claude-opus"
    }


@pytest.mark.unit
@pytest.mark.parametrize("task_type", _GOLDEN)
def test_live_policy_tier_orders_are_unchanged(task_type: str) -> None:
    expected = {
        "summarization": ["local", "cheap_frontier", "cheap_cloud"],
        "document": ["local", "cheap_frontier", "cheap_cloud"],
        "code_review": [],  # Withheld: the shipped policy declares no tier_order.
        "test": ["local", "cheap_frontier", "cheap_cloud", "claude"],
        "code_generation": ["local", "cheap_frontier", "cheap_cloud", "claude"],
        "reasoning": ["local", "cheap_frontier", "cheap_cloud"],
    }
    classes = yaml.safe_load(_CONTRACT.read_text())["task_classes"]
    assert (
        classes[task_type]["escalation_policy"].get("tier_order", [])
        == expected[task_type]
    )


@pytest.mark.unit
@pytest.mark.parametrize("task_type", _GOLDEN)
def test_default_resolver_reads_real_configs(
    task_type: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(_BIFROST))
    monkeypatch.delenv("BIFROST_OVERLAY_PATH", raising=False)
    monkeypatch.setenv("DELEGATION_ROUTING_TIERS_PATH", str(_TIERS))
    monkeypatch.setenv("TASK_CLASS_CONTRACT_PATH", str(_CONTRACT))
    monkeypatch.setattr(routing, "_config", None)
    routing._get_task_class_contract.cache_clear()
    try:
        chain = resolve_class_escalation_chain(
            task_type,
            tenant_id="omninode",
            surface=EnumDelegationBackendSurface.INTERNAL,
        )
        assert chain is not None
        assert (
            tuple((rung.tier, rung.model_name) for rung in chain.rungs)
            == _GOLDEN[task_type]
        )
        for rung in chain.rungs:
            assert rung.refusals == (
                (EnumHarnessRungRefusal.NOT_PIN_ROUTABLE,)
                if rung.kind == "harness"
                else ()
            )
    finally:
        routing._config = None
        routing._get_task_class_contract.cache_clear()
