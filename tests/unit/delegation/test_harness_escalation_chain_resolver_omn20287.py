# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""House harness chain resolution (OMN-20287).

Exercise forged declarations without depending on producer YAML changes.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from omnibase_infra.errors import ProtocolConfigurationError
from pydantic import ValidationError

from omnimarket.enums import EnumHarnessRungRefusal
from omnimarket.models.delegation.model_class_escalation_chain import (
    ModelClassEscalationChain,
)
from omnimarket.models.delegation.model_harness_tier import ModelHarnessTier
from omnimarket.models.delegation.model_resolved_escalation_chain import (
    ModelResolvedEscalationChain,
)
from omnimarket.models.delegation.wire.model_bifrost_delegation_config import (
    EnumDelegationBackendKind,
    EnumDelegationBackendSurface,
    EnumDelegationBackendTenantScope,
    EnumDelegationHarness,
    ModelDelegationBackendConfig,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
    handler_delegation_routing as routing,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_harness_escalation_chain import (
    resolve_class_escalation_chain,
)
from omnimarket.routing.routing_tiers_path import load_harness_tiers

_CONFIGS = Path(__file__).resolve().parents[3] / "src/omnimarket/configs"
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


# These inputs belong to the consumer tests; the shipped configs declare no chains.
_FORGED_BACKENDS = tuple(
    ModelDelegationBackendConfig(
        backend_id=backend_id,
        provider=provider,
        kind=EnumDelegationBackendKind.HARNESS,
        harness=harness,
        surface=EnumDelegationBackendSurface.INTERNAL,
        tenant_scope=EnumDelegationBackendTenantScope.HOUSE,
        model_name=model_name,
        tier="harness",
        explicit_pin_only=True,
    )
    for backend_id, provider, harness, model_name in (
        ("harness-codex", "openai", EnumDelegationHarness.CODEX, None),
        (
            "harness-claude-glm-flash",
            "zai",
            EnumDelegationHarness.CLAUDE_GLM,
            "glm-5.3-flash",
        ),
        ("harness-claude-glm", "zai", EnumDelegationHarness.CLAUDE_GLM, "glm-5.3"),
        (
            "harness-claude-haiku",
            "anthropic",
            EnumDelegationHarness.CLAUDE,
            "claude-haiku-5-5",
        ),
        (
            "harness-claude-sonnet",
            "anthropic",
            EnumDelegationHarness.CLAUDE,
            "claude-sonnet-5-5",
        ),
        (
            "harness-claude-opus",
            "anthropic",
            EnumDelegationHarness.CLAUDE,
            "claude-opus-5-5",
        ),
    )
)
_FORGED_HARNESS_TIERS = (
    ModelHarnessTier(
        name="harness_codex",
        backend_id="harness-codex",
        use_for=("code_review", "test", "code_generation"),
    ),
    ModelHarnessTier(
        name="harness_glm_flash",
        backend_id="harness-claude-glm-flash",
        use_for=("document",),
    ),
    ModelHarnessTier(
        name="harness_glm",
        backend_id="harness-claude-glm",
        use_for=("document", "code_review", "reasoning"),
    ),
    ModelHarnessTier(
        name="harness_haiku",
        backend_id="harness-claude-haiku",
        use_for=("document", "test"),
    ),
    ModelHarnessTier(
        name="harness_sonnet",
        backend_id="harness-claude-sonnet",
        use_for=(
            "summarization",
            "document",
            "code_review",
            "test",
            "code_generation",
            "reasoning",
        ),
    ),
)
_FORGED_TASK_CLASS_CONTRACT: dict[str, object] = {
    "task_classes": {
        task_type: {
            "escalation_chain": {"escalate_on": "acceptance_check", "rungs": rungs}
        }
        for task_type, rungs in (
            ("summarization", ["local", "harness_sonnet"]),
            (
                "document",
                [
                    "local",
                    "harness_glm_flash",
                    "harness_glm",
                    "harness_haiku",
                    "harness_sonnet",
                ],
            ),
            ("code_review", ["harness_codex", "harness_glm", "harness_sonnet"]),
            ("test", ["harness_codex", "harness_haiku", "harness_sonnet"]),
            ("code_generation", ["harness_codex", "local", "harness_sonnet"]),
            ("reasoning", ["local", "harness_glm", "harness_sonnet"]),
        )
    }
}


@pytest.fixture
def pinned_backends() -> tuple[ModelDelegationBackendConfig, ...]:
    return _FORGED_BACKENDS


@pytest.fixture
def unpinned_backends() -> tuple[ModelDelegationBackendConfig, ...]:
    return tuple(
        ModelDelegationBackendConfig.model_validate(
            {**backend.model_dump(), "explicit_pin_only": False}
        )
        for backend in _FORGED_BACKENDS
    )


@pytest.fixture
def forged_tiers() -> tuple[ModelHarnessTier, ...]:
    return _FORGED_HARNESS_TIERS


@pytest.fixture
def forged_contract() -> dict[str, object]:
    return _FORGED_TASK_CLASS_CONTRACT


@pytest.fixture
def ladder_names() -> frozenset[str]:
    return frozenset({"local"})


def _resolve(
    task_type: str,
    backends: tuple[ModelDelegationBackendConfig, ...],
    tiers: tuple[ModelHarnessTier, ...],
    contract: dict[str, object],
    ladder: frozenset[str],
    *,
    tenant_id: str | None = "omninode",
    surface: EnumDelegationBackendSurface = EnumDelegationBackendSurface.INTERNAL,
) -> ModelResolvedEscalationChain:
    chain = resolve_class_escalation_chain(
        task_type,
        tenant_id=tenant_id,
        surface=surface,
        backends=backends,
        harness_tiers=tiers,
        task_class_contract=contract,
        ladder_tier_names=ladder,
    )
    assert chain is not None
    return chain


@pytest.mark.unit
@pytest.mark.parametrize("task_type", _GOLDEN)
def test_forged_chain_order_and_model_names(
    task_type: str,
    unpinned_backends: tuple[ModelDelegationBackendConfig, ...],
    forged_tiers: tuple[ModelHarnessTier, ...],
    forged_contract: dict[str, object],
    ladder_names: frozenset[str],
) -> None:
    chain = _resolve(
        task_type, unpinned_backends, forged_tiers, forged_contract, ladder_names
    )
    assert chain.task_type == task_type
    assert chain.escalate_on == "acceptance_check"
    assert (
        tuple((rung.tier, rung.model_name) for rung in chain.rungs)
        == _GOLDEN[task_type]
    )
    assert all(rung.selectable and not rung.refusals for rung in chain.rungs)
    backend_ids = {tier.name: tier.backend_id for tier in forged_tiers}
    for rung in chain.rungs:
        assert rung.kind == ("ladder" if rung.tier == "local" else "harness")
        assert rung.backend_id == backend_ids.get(rung.tier)


@pytest.mark.unit
@pytest.mark.parametrize("task_type", _GOLDEN)
def test_pinned_harness_rungs_are_refused(
    task_type: str,
    pinned_backends: tuple[ModelDelegationBackendConfig, ...],
    forged_tiers: tuple[ModelHarnessTier, ...],
    forged_contract: dict[str, object],
    ladder_names: frozenset[str],
) -> None:
    assert all(backend.explicit_pin_only for backend in pinned_backends)
    chain = _resolve(
        task_type, pinned_backends, forged_tiers, forged_contract, ladder_names
    )
    for rung in chain.rungs:
        assert rung.refusals == (
            (EnumHarnessRungRefusal.NOT_PIN_ROUTABLE,) if rung.kind == "harness" else ()
        )
        assert rung.selectable == (rung.kind == "ladder")


@pytest.mark.unit
def test_class_without_chain_returns_none() -> None:
    assert (
        resolve_class_escalation_chain(
            "refactor",
            tenant_id="omninode",
            surface=EnumDelegationBackendSurface.INTERNAL,
            backends=_FORGED_BACKENDS,
            harness_tiers=_FORGED_HARNESS_TIERS,
            task_class_contract={"task_classes": {"refactor": {}}},
            ladder_tier_names=frozenset({"local"}),
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
            EnumDelegationBackendSurface.INTERNAL,
            EnumHarnessRungRefusal.NOT_HOUSE_TENANT,
        ),
        (
            None,
            EnumDelegationBackendSurface.INTERNAL,
            EnumHarnessRungRefusal.NOT_HOUSE_TENANT,
        ),
        (
            "omninode",
            EnumDelegationBackendSurface.ANY,
            EnumHarnessRungRefusal.NOT_INTERNAL_SURFACE,
        ),
    ],
)
def test_unpinned_harness_refuses_wrong_tenant_or_surface(
    task_type: str,
    tenant_id: str | None,
    surface: EnumDelegationBackendSurface,
    refusal: EnumHarnessRungRefusal,
    unpinned_backends: tuple[ModelDelegationBackendConfig, ...],
    forged_tiers: tuple[ModelHarnessTier, ...],
    forged_contract: dict[str, object],
    ladder_names: frozenset[str],
) -> None:
    chain = _resolve(
        task_type,
        unpinned_backends,
        forged_tiers,
        forged_contract,
        ladder_names,
        tenant_id=tenant_id,
        surface=surface,
    )
    for rung in chain.rungs:
        assert rung.refusals == ((refusal,) if rung.kind == "harness" else ())
        assert rung.selectable == (rung.kind == "ladder")


@pytest.mark.unit
def test_refusals_accumulate_in_contract_order(
    pinned_backends: tuple[ModelDelegationBackendConfig, ...],
    forged_tiers: tuple[ModelHarnessTier, ...],
    forged_contract: dict[str, object],
    ladder_names: frozenset[str],
) -> None:
    chain = _resolve(
        "document",
        pinned_backends,
        forged_tiers,
        forged_contract,
        ladder_names,
        tenant_id=None,
        surface=EnumDelegationBackendSurface.ANY,
    )
    for rung in chain.rungs:
        assert rung.refusals == (
            (
                EnumHarnessRungRefusal.NOT_PIN_ROUTABLE,
                EnumHarnessRungRefusal.NOT_HOUSE_TENANT,
                EnumHarnessRungRefusal.NOT_INTERNAL_SURFACE,
            )
            if rung.kind == "harness"
            else ()
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
        ModelClassEscalationChain.model_validate(chain)
    with pytest.raises(ValidationError):
        resolve_class_escalation_chain(
            "document",
            tenant_id="omninode",
            surface=EnumDelegationBackendSurface.INTERNAL,
            task_class_contract={
                "task_classes": {"document": {"escalation_chain": chain}}
            },
            backends=_FORGED_BACKENDS,
            harness_tiers=_FORGED_HARNESS_TIERS,
            ladder_tier_names=frozenset({"local"}),
        )


@pytest.mark.unit
@pytest.mark.parametrize("error", ["unknown_rung", "wrong_class", "undeclared_backend"])
def test_chain_reference_errors_name_class_and_rung(
    error: str,
    unpinned_backends: tuple[ModelDelegationBackendConfig, ...],
    forged_tiers: tuple[ModelHarnessTier, ...],
    ladder_names: frozenset[str],
) -> None:
    rung = "harness_unknown" if error == "unknown_rung" else "harness_sonnet"
    tiers = forged_tiers
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
            for tier in forged_tiers
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
        _resolve("document", unpinned_backends, tiers, contract, ladder_names)


@pytest.mark.unit
@pytest.mark.parametrize(
    "update",
    [{"name": "local"}, {"backend_id": ""}, {"use_for": []}],
)
def test_harness_tier_constraints(
    update: dict[str, object],
    forged_tiers: tuple[ModelHarnessTier, ...],
) -> None:
    with pytest.raises(ValidationError):
        ModelHarnessTier.model_validate({**forged_tiers[0].model_dump(), **update})


@pytest.mark.unit
def test_models_are_frozen_and_forbid_extra_fields(
    pinned_backends: tuple[ModelDelegationBackendConfig, ...],
    forged_tiers: tuple[ModelHarnessTier, ...],
    forged_contract: dict[str, object],
    ladder_names: frozenset[str],
) -> None:
    resolved = _resolve(
        "document", pinned_backends, forged_tiers, forged_contract, ladder_names
    )
    models = (
        pinned_backends[0],
        forged_tiers[0],
        ModelClassEscalationChain(escalate_on="acceptance_check", rungs=("local",)),
        resolved,
        *resolved.rungs,
    )
    for model in models:
        with pytest.raises(ValidationError):
            type(model).model_validate({**model.model_dump(), "unexpected": True})
        field = next(iter(type(model).model_fields))
        with pytest.raises(ValidationError):
            setattr(model, field, getattr(model, field))
    assert pinned_backends[0].harness is EnumDelegationHarness.CODEX


@pytest.mark.unit
@pytest.mark.parametrize("error", ["duplicate", "non_list", "invalid_entry"])
def test_tier_loader_refuses_invalid_blocks(tmp_path: Path, error: str) -> None:
    data: dict[str, object] = {
        "harness_tiers": [
            tier.model_dump(mode="json") for tier in _FORGED_HARNESS_TIERS
        ]
    }
    if error == "duplicate":
        data["harness_tiers"] = [
            _FORGED_HARNESS_TIERS[0].model_dump(mode="json"),
            _FORGED_HARNESS_TIERS[0].model_dump(mode="json"),
        ]
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
def test_harness_tiers_name_only_declared_harness_backends(
    pinned_backends: tuple[ModelDelegationBackendConfig, ...],
    forged_tiers: tuple[ModelHarnessTier, ...],
) -> None:
    assert len(pinned_backends) == 6
    assert len(forged_tiers) == 5
    assert {tier.backend_id for tier in forged_tiers} == {
        backend.backend_id
        for backend in pinned_backends
        if backend.backend_id != "harness-claude-opus"
    }


@pytest.mark.unit
def test_harness_tier_cannot_name_endpoint_backend(
    forged_tiers: tuple[ModelHarnessTier, ...],
    forged_contract: dict[str, object],
    ladder_names: frozenset[str],
) -> None:
    endpoint = ModelDelegationBackendConfig(
        backend_id="harness-claude-sonnet", provider="openai", tier="cheap_cloud"
    )
    with pytest.raises(
        ProtocolConfigurationError, match=r"summarization.*harness_sonnet.*harness"
    ):
        _resolve(
            "summarization", (endpoint,), forged_tiers, forged_contract, ladder_names
        )


@pytest.mark.unit
def test_shipped_configs_have_no_escalation_chains_or_harness_tiers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TASK_CLASS_CONTRACT_PATH", str(_CONTRACT))
    monkeypatch.setenv("DELEGATION_ROUTING_TIERS_PATH", str(_TIERS))
    routing._get_task_class_contract.cache_clear()
    try:
        for task_type in _GOLDEN:
            assert (
                resolve_class_escalation_chain(
                    task_type,
                    tenant_id="omninode",
                    surface=EnumDelegationBackendSurface.INTERNAL,
                )
                is None
            ), task_type
        assert load_harness_tiers() == ()
    finally:
        routing._get_task_class_contract.cache_clear()
