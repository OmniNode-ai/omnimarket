# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""House harness chains and live-ladder isolation (OMN-20287).

Exercise the shipped contracts at config-resolution level only: no network I/O.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_infra.errors import ProtocolConfigurationError
from pydantic import ValidationError

from omnimarket.adapters.llm.bifrost.config_loader_bifrost_delegation import (
    load_bifrost_delegation_config,
    load_harness_backends,
)
from omnimarket.enums import EnumHarness, EnumHarnessRungRefusal, EnumHarnessSurface
from omnimarket.models.delegation.model_class_escalation_chain import (
    ModelClassEscalationChain,
)
from omnimarket.models.delegation.model_harness_backend import ModelHarnessBackend
from omnimarket.models.delegation.model_harness_tier import ModelHarnessTier
from omnimarket.models.delegation.model_resolved_escalation_chain import (
    ModelResolvedEscalationChain,
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
from omnimarket.routing.customer_key_terminus import EnumDelegationSurface
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
def shipped_backends() -> tuple[ModelHarnessBackend, ...]:
    return load_harness_backends(_BIFROST)


@pytest.fixture
def bound_backends(
    shipped_backends: tuple[ModelHarnessBackend, ...],
) -> tuple[ModelHarnessBackend, ...]:
    return tuple(
        backend.model_copy(update={"executor_bound": True})
        for backend in shipped_backends
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
    backends: tuple[ModelHarnessBackend, ...],
    tiers: tuple[ModelHarnessTier, ...],
    contract: dict[str, object],
    ladder: frozenset[str],
    *,
    tenant_id: str | None = "omninode",
    surface: EnumDelegationSurface | EnumHarnessSurface = EnumHarnessSurface.INTERNAL,
) -> ModelResolvedEscalationChain:
    chain = resolve_class_escalation_chain(
        task_type,
        tenant_id=tenant_id,
        surface=surface,
        harness_backends=backends,
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
    bound_backends: tuple[ModelHarnessBackend, ...],
    shipped_tiers: tuple[ModelHarnessTier, ...],
    shipped_contract: dict[str, object],
    ladder_names: frozenset[str],
) -> None:
    chain = _resolve(
        task_type, bound_backends, shipped_tiers, shipped_contract, ladder_names
    )
    assert chain.task_type == task_type
    assert chain.escalate_on == "acceptance_check"
    assert (
        tuple((rung.tier, rung.model_name) for rung in chain.rungs)
        == _GOLDEN[task_type]
    )
    assert all(rung.selectable and not rung.refusals for rung in chain.rungs)
    backend_ids = {tier.name: tier.backend_id for tier in shipped_tiers}
    for rung in chain.rungs:
        assert rung.kind == ("ladder" if rung.tier == "local" else "harness")
        assert rung.backend_id == backend_ids.get(rung.tier)


@pytest.mark.unit
@pytest.mark.parametrize("task_type", _GOLDEN)
def test_shipped_executors_are_unbound(
    task_type: str,
    shipped_backends: tuple[ModelHarnessBackend, ...],
    shipped_tiers: tuple[ModelHarnessTier, ...],
    shipped_contract: dict[str, object],
    ladder_names: frozenset[str],
) -> None:
    assert all(not backend.executor_bound for backend in shipped_backends)
    chain = _resolve(
        task_type, shipped_backends, shipped_tiers, shipped_contract, ladder_names
    )
    for rung in chain.rungs:
        assert rung.refusals == (
            (EnumHarnessRungRefusal.EXECUTOR_UNBOUND,) if rung.kind == "harness" else ()
        )
        assert rung.selectable == (rung.kind == "ladder")


@pytest.mark.unit
def test_class_without_chain_returns_none() -> None:
    assert (
        resolve_class_escalation_chain(
            "refactor", tenant_id="omninode", surface=EnumHarnessSurface.INTERNAL
        )
        is None
    )


@pytest.mark.unit
@pytest.mark.parametrize("task_type", _GOLDEN)
@pytest.mark.parametrize(
    ("tenant_id", "surface", "refusal"),
    [
        (
            "acme-corp",
            EnumHarnessSurface.INTERNAL,
            EnumHarnessRungRefusal.NOT_HOUSE_TENANT,
        ),
        (None, EnumHarnessSurface.INTERNAL, EnumHarnessRungRefusal.NOT_HOUSE_TENANT),
        (
            "omninode",
            EnumDelegationSurface.CLOUD,
            EnumHarnessRungRefusal.NOT_INTERNAL_SURFACE,
        ),
        (
            "omninode",
            EnumDelegationSurface.CUSTOMER_LOCAL,
            EnumHarnessRungRefusal.NOT_INTERNAL_SURFACE,
        ),
    ],
)
def test_bound_harness_refuses_wrong_tenant_or_surface(
    task_type: str,
    tenant_id: str | None,
    surface: EnumDelegationSurface | EnumHarnessSurface,
    refusal: EnumHarnessRungRefusal,
    bound_backends: tuple[ModelHarnessBackend, ...],
    shipped_tiers: tuple[ModelHarnessTier, ...],
    shipped_contract: dict[str, object],
    ladder_names: frozenset[str],
) -> None:
    chain = _resolve(
        task_type,
        bound_backends,
        shipped_tiers,
        shipped_contract,
        ladder_names,
        tenant_id=tenant_id,
        surface=surface,
    )
    for rung in chain.rungs:
        assert rung.refusals == ((refusal,) if rung.kind == "harness" else ())
        assert rung.selectable == (rung.kind == "ladder")


@pytest.mark.unit
def test_refusals_accumulate_in_contract_order(
    shipped_backends: tuple[ModelHarnessBackend, ...],
    shipped_tiers: tuple[ModelHarnessTier, ...],
    shipped_contract: dict[str, object],
    ladder_names: frozenset[str],
) -> None:
    chain = _resolve(
        "document",
        shipped_backends,
        shipped_tiers,
        shipped_contract,
        ladder_names,
        tenant_id=None,
        surface=EnumDelegationSurface.CLOUD,
    )
    for rung in chain.rungs:
        assert rung.refusals == (
            tuple(EnumHarnessRungRefusal) if rung.kind == "harness" else ()
        )


@pytest.mark.unit
@pytest.mark.parametrize(
    "chain",
    [
        {"escalate_on": "timeout", "rungs": ["local"]},
        {"escalate_on": "acceptance_check", "rungs": ["local", "local"]},
        {"escalate_on": "acceptance_check", "rungs": []},
    ],
)
def test_invalid_chain_is_validated(chain: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        resolve_class_escalation_chain(
            "document",
            tenant_id="omninode",
            surface=EnumHarnessSurface.INTERNAL,
            task_class_contract={
                "task_classes": {"document": {"escalation_chain": chain}}
            },
        )


@pytest.mark.unit
@pytest.mark.parametrize("error", ["unknown_rung", "wrong_class", "undeclared_backend"])
def test_chain_reference_errors_name_class_and_rung(
    error: str,
    bound_backends: tuple[ModelHarnessBackend, ...],
    shipped_tiers: tuple[ModelHarnessTier, ...],
    ladder_names: frozenset[str],
) -> None:
    rung = "harness_unknown" if error == "unknown_rung" else "harness_sonnet"
    tiers = shipped_tiers
    if error != "unknown_rung":
        tiers = tuple(
            tier.model_copy(
                update={
                    "use_for": ("test",) if error == "wrong_class" else tier.use_for,
                    "backend_id": "missing"
                    if error == "undeclared_backend"
                    else tier.backend_id,
                }
            )
            if tier.name == rung
            else tier
            for tier in shipped_tiers
        )
    contract: dict[str, object] = {
        "task_classes": {
            "document": {
                "escalation_chain": {
                    "escalate_on": "acceptance_check",
                    "rungs": [rung],
                }
            }
        }
    }
    with pytest.raises(ProtocolConfigurationError, match=f"document.*{rung}"):
        _resolve("document", bound_backends, tiers, contract, ladder_names)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("surface", "cloud"),
        ("tenant", "acme-corp"),
        ("kind", "endpoint"),
        ("executor", "other"),
        ("budget_seconds", 0),
        ("backend_id", ""),
        ("terms", ""),
    ],
)
def test_backend_model_refuses_invalid_declarations(
    field: str,
    value: object,
    shipped_backends: tuple[ModelHarnessBackend, ...],
) -> None:
    data = shipped_backends[0].model_dump()
    data[field] = value
    with pytest.raises(ValidationError):
        ModelHarnessBackend.model_validate(data)


@pytest.mark.unit
@pytest.mark.parametrize(
    "update",
    [{"name": "local"}, {"backend_id": ""}, {"use_for": []}],
)
def test_harness_tier_constraints(
    update: dict[str, object],
    shipped_tiers: tuple[ModelHarnessTier, ...],
) -> None:
    with pytest.raises(ValidationError):
        ModelHarnessTier.model_validate({**shipped_tiers[0].model_dump(), **update})


@pytest.mark.unit
def test_models_are_frozen_and_forbid_extra_fields(
    shipped_backends: tuple[ModelHarnessBackend, ...],
    shipped_tiers: tuple[ModelHarnessTier, ...],
) -> None:
    models = (
        shipped_backends[0],
        shipped_tiers[0],
        ModelClassEscalationChain(escalate_on="acceptance_check", rungs=("local",)),
    )
    for model in models:
        with pytest.raises(ValidationError):
            type(model).model_validate({**model.model_dump(), "unexpected": True})
        field = next(iter(type(model).model_fields))
        with pytest.raises(ValidationError):
            setattr(model, field, getattr(model, field))
    assert shipped_backends[0].harness is EnumHarness.CODEX


def _write_bifrost(tmp_path: Path, data: dict[str, Any]) -> Path:
    path = tmp_path / "bifrost.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


@pytest.mark.unit
@pytest.mark.parametrize("error", ["http_collision", "duplicate"])
def test_backend_loader_refuses_colliding_ids(tmp_path: Path, error: str) -> None:
    data = yaml.safe_load(_BIFROST.read_text())
    if error == "http_collision":
        data["harness_backends"][0]["backend_id"] = data["backends"][0]["backend_id"]
    else:
        data["harness_backends"].append(data["harness_backends"][0].copy())
    backend_id = data["harness_backends"][0]["backend_id"]
    path = _write_bifrost(tmp_path, data)
    with pytest.raises(ValueError, match=r"[Bb]ackend") as exc:
        load_harness_backends(path)
    assert backend_id in str(exc.value)
    assert str(path) in str(exc.value)


@pytest.mark.unit
@pytest.mark.parametrize(
    "block", [None, {}, "invalid", [42], [{"backend_id": "broken"}]]
)
def test_backend_loader_refuses_malformed_blocks(tmp_path: Path, block: object) -> None:
    data = yaml.safe_load(_BIFROST.read_text())
    data["harness_backends"] = block
    path = _write_bifrost(tmp_path, data)
    with pytest.raises(ValueError, match=r"[Bb]ackend") as exc:
        load_harness_backends(path)
    assert str(path) in str(exc.value)
    if block == [{"backend_id": "broken"}]:
        assert "broken" in str(exc.value)


@pytest.mark.unit
def test_backend_loader_absent_block(tmp_path: Path) -> None:
    data = yaml.safe_load(_BIFROST.read_text())
    del data["harness_backends"]
    assert load_harness_backends(_write_bifrost(tmp_path, data)) == ()


@pytest.mark.unit
def test_backend_loader_merges_overlay(tmp_path: Path) -> None:
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text(
        yaml.safe_dump(
            {
                "harness_backends": [
                    {"backend_id": "harness-codex", "executor_bound": True},
                ]
            }
        )
    )
    backends = load_harness_backends(_BIFROST, overlay)
    assert len(backends) == 5
    assert backends[0].executor_bound
    assert all(not backend.executor_bound for backend in backends[1:])


@pytest.mark.unit
@pytest.mark.parametrize("error", ["duplicate", "non_list", "invalid_entry"])
def test_tier_loader_refuses_invalid_blocks(tmp_path: Path, error: str) -> None:
    data = yaml.safe_load(_TIERS.read_text())
    if error == "duplicate":
        data["harness_tiers"].append(data["harness_tiers"][0].copy())
    elif error == "non_list":
        data["harness_tiers"] = None
    else:
        data["harness_tiers"] = [{"name": "harness_broken"}]
    path = tmp_path / "tiers.yaml"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError, match=r"[Tt]ier") as exc:
        load_harness_tiers(path)
    assert str(path) in str(exc.value)
    if error == "duplicate":
        assert "harness_codex" in str(exc.value)


@pytest.mark.unit
def test_tier_loader_absent_block(tmp_path: Path) -> None:
    path = tmp_path / "tiers.yaml"
    path.write_text("{}")
    assert load_harness_tiers(path) == ()


@pytest.mark.unit
def test_bifrost_wire_config_contains_no_harness_backends(
    shipped_backends: tuple[ModelHarnessBackend, ...],
) -> None:
    config = load_bifrost_delegation_config(_BIFROST)
    assert config.backends
    assert {backend.backend_id for backend in shipped_backends}.isdisjoint(
        backend.backend_id for backend in config.backends
    )


@pytest.mark.unit
def test_live_endpoints_contain_no_harness_backends(
    shipped_backends: tuple[ModelHarnessBackend, ...],
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
def test_harness_backends_and_tiers_are_one_to_one(
    shipped_backends: tuple[ModelHarnessBackend, ...],
    shipped_tiers: tuple[ModelHarnessTier, ...],
) -> None:
    assert len(shipped_backends) == len(shipped_tiers) == 5
    counts = Counter(tier.backend_id for tier in shipped_tiers)
    assert counts == Counter({backend.backend_id: 1 for backend in shipped_backends})
