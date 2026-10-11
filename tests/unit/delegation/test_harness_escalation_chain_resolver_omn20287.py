# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Synthetic harness overlay resolution and refusal coverage (OMN-20287)."""

from __future__ import annotations

import pytest
from omnibase_infra.errors import ProtocolConfigurationError
from pydantic import ValidationError

from omnimarket.enums import EnumHarnessRungRefusal
from omnimarket.models.delegation.model_class_escalation_chain import (
    ModelClassEscalationChain,
)
from omnimarket.models.delegation.model_delegation_routing_overlay import (
    ModelDelegationRoutingOverlay,
    ModelTaskClassEscalationChain,
)
from omnimarket.models.delegation.model_harness_tier import ModelHarnessTier
from omnimarket.models.delegation.wire.model_bifrost_delegation_config import (
    EnumDelegationBackendSurface,
    ModelDelegationBackendConfig,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_harness_escalation_chain import (
    resolve_class_escalation_chain,
)
from omnimarket.routing.routing_tiers_path import load_harness_tiers

pytestmark = pytest.mark.unit


@pytest.fixture
def overlay() -> ModelDelegationRoutingOverlay:
    return ModelDelegationRoutingOverlay(
        schema_version="delegation_routing_overlay.v1",
        harness_backends=tuple(
            ModelDelegationBackendConfig(
                backend_id=f"harness-test-{suffix}",
                provider="example-provider",
                kind="harness",
                harness="claude",
                surface="internal",
                tenant_scope="house",
                model_name=f"example-model-{suffix}",
                tier="harness",
            )
            for suffix in ("a", "b")
        ),
        harness_tiers=tuple(
            ModelHarnessTier(
                name=f"harness_{suffix}",
                backend_id=f"harness-test-{suffix}",
                use_for=("sample_task", "other_task"),
            )
            for suffix in ("a", "b")
        ),
        escalation_chains=(
            ModelTaskClassEscalationChain(
                task_class="sample_task",
                escalate_on="acceptance_check",
                rungs=("harness_b", "local", "harness_a"),
            ),
            ModelTaskClassEscalationChain(
                task_class="other_task",
                escalate_on="acceptance_check",
                rungs=("harness_a", "harness_b", "local"),
            ),
        ),
    )


@pytest.mark.parametrize(
    ("task_class", "expected"),
    [
        ("sample_task", ("harness_b", "local", "harness_a")),
        ("other_task", ("harness_a", "harness_b", "local")),
    ],
)
def test_order_and_ladder_vs_harness_rungs(
    overlay: ModelDelegationRoutingOverlay, task_class: str, expected: tuple[str, ...]
) -> None:
    chain = resolve_class_escalation_chain(
        task_class,
        tenant_id="omninode",
        surface=EnumDelegationBackendSurface.INTERNAL,
        overlay=overlay,
        ladder_tier_names=frozenset({"local"}),
    )
    assert chain is not None
    assert chain.task_type == task_class
    assert chain.escalate_on == "acceptance_check"
    assert tuple(rung.tier for rung in chain.rungs) == expected
    for rung in chain.rungs:
        assert rung.kind == ("ladder" if rung.tier == "local" else "harness")
        assert rung.selectable
        assert rung.refusals == ()
        if rung.kind == "harness":
            suffix = rung.tier.removeprefix("harness_")
            assert rung.backend_id == f"harness-test-{suffix}"
            assert rung.model_name == f"example-model-{suffix}"
        else:
            assert rung.backend_id is None
            assert rung.model_name is None
    assert load_harness_tiers(overlay) == overlay.harness_tiers
    assert overlay.chain_for("absent") is None


@pytest.mark.parametrize("pin_only", [False, True])
@pytest.mark.parametrize("tenant_id", ["omninode", "example-tenant", None])
@pytest.mark.parametrize("surface", list(EnumDelegationBackendSurface))
def test_every_refusal_combination(
    overlay: ModelDelegationRoutingOverlay,
    pin_only: bool,
    tenant_id: str | None,
    surface: EnumDelegationBackendSurface,
) -> None:
    overlay = overlay.model_copy(
        update={
            "harness_backends": tuple(
                b.model_copy(update={"explicit_pin_only": pin_only})
                for b in overlay.harness_backends
            )
        }
    )
    chain = resolve_class_escalation_chain(
        "sample_task",
        tenant_id=tenant_id,
        surface=surface,
        overlay=overlay,
        ladder_tier_names=frozenset({"local"}),
    )
    assert chain is not None
    expected = tuple(
        refusal
        for condition, refusal in (
            (pin_only, EnumHarnessRungRefusal.NOT_PIN_ROUTABLE),
            (tenant_id != "omninode", EnumHarnessRungRefusal.NOT_HOUSE_TENANT),
            (
                surface is not EnumDelegationBackendSurface.INTERNAL,
                EnumHarnessRungRefusal.NOT_INTERNAL_SURFACE,
            ),
        )
        if condition
    )
    for rung in chain.rungs:
        assert rung.refusals == (expected if rung.kind == "harness" else ())
        assert rung.selectable == (not rung.refusals)


def test_class_without_chain(overlay: ModelDelegationRoutingOverlay) -> None:
    assert (
        resolve_class_escalation_chain(
            "absent",
            tenant_id=None,
            surface=EnumDelegationBackendSurface.ANY,
            overlay=overlay,
            ladder_tier_names=frozenset(),
        )
        is None
    )


@pytest.mark.parametrize("error", ["unknown", "use_for", "backend", "kind"])
def test_resolver_defensive_reference_errors(
    overlay: ModelDelegationRoutingOverlay, error: str
) -> None:
    # model_copy deliberately bypasses load-time validation to exercise the
    # resolver's retained defensive checks for forged in-memory declarations.
    if error == "unknown":
        overlay = overlay.model_copy(
            update={
                "escalation_chains": (
                    ModelTaskClassEscalationChain(
                        task_class="sample_task",
                        escalate_on="acceptance_check",
                        rungs=("unknown_ladder",),
                    ),
                )
            }
        )
        match = "sample_task.*unknown_ladder"
    elif error in {"use_for", "backend"}:
        tier = overlay.harness_tiers[1].model_copy(
            update={
                "use_for": ("other_task",) if error == "use_for" else ("sample_task",),
                "backend_id": "missing-backend"
                if error == "backend"
                else "harness-test-b",
            }
        )
        overlay = overlay.model_copy(
            update={"harness_tiers": (overlay.harness_tiers[0], tier)}
        )
        match = "sample_task.*harness_b"
    else:
        endpoint = ModelDelegationBackendConfig(
            backend_id="harness-test-b", tier="local"
        )
        overlay = overlay.model_copy(
            update={"harness_backends": (overlay.harness_backends[0], endpoint)}
        )
        match = "sample_task.*harness_b.*kind is not harness"
    with pytest.raises(ProtocolConfigurationError, match=match):
        resolve_class_escalation_chain(
            "sample_task",
            tenant_id="omninode",
            surface=EnumDelegationBackendSurface.INTERNAL,
            overlay=overlay,
            ladder_tier_names=frozenset({"local"}),
        )


def test_harness_tier_collision_with_selected_ladder(
    overlay: ModelDelegationRoutingOverlay,
) -> None:
    with pytest.raises(
        ProtocolConfigurationError, match=r"collide.*harness_a.*harness_b"
    ):
        resolve_class_escalation_chain(
            "sample_task",
            tenant_id="omninode",
            surface=EnumDelegationBackendSurface.INTERNAL,
            overlay=overlay,
            ladder_tier_names=frozenset({"local", "harness_a", "harness_b"}),
        )


@pytest.mark.parametrize(
    "update",
    [
        {"task_class": ""},
        {"escalate_on": "timeout"},
        {"rungs": []},
        {"rungs": ["local", "local"]},
        {"unexpected": True},
    ],
)
def test_task_class_chain_constraints(update: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ModelTaskClassEscalationChain.model_validate(
            {
                "task_class": "sample_task",
                "escalate_on": "acceptance_check",
                "rungs": ["local"],
                **update,
            }
        )


@pytest.mark.parametrize(
    "update", [{"name": "local"}, {"backend_id": ""}, {"use_for": []}]
)
def test_harness_tier_constraints(
    overlay: ModelDelegationRoutingOverlay, update: dict[str, object]
) -> None:
    with pytest.raises(ValidationError):
        ModelHarnessTier.model_validate(
            {**overlay.harness_tiers[0].model_dump(), **update}
        )


def test_models_frozen_and_forbid_extras(
    overlay: ModelDelegationRoutingOverlay,
) -> None:
    chain = resolve_class_escalation_chain(
        "sample_task",
        tenant_id="omninode",
        surface=EnumDelegationBackendSurface.INTERNAL,
        overlay=overlay,
        ladder_tier_names=frozenset({"local"}),
    )
    assert chain is not None
    models = (
        overlay,
        *overlay.harness_backends,
        *overlay.harness_tiers,
        *overlay.escalation_chains,
        ModelClassEscalationChain(escalate_on="acceptance_check", rungs=("local",)),
        chain,
        *chain.rungs,
    )
    for model in models:
        with pytest.raises(ValidationError):
            type(model).model_validate({**model.model_dump(), "unexpected": True})
        field = next(iter(type(model).model_fields))
        with pytest.raises(ValidationError):
            setattr(model, field, getattr(model, field))
