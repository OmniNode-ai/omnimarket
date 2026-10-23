# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Onboarding's recommended model order per work class, in the routing contract (OMN-21209).

Every served work class (a task class with no routing_availability) resolves a
non-empty order for each offered key and for Ollama, and names the record it
rests on. Accepting the order registers it through the OMN-21059 path, and the
key's route walks exactly that order. Fails before OMN-21209: the catalogue has
no recommended_order.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import SecretStr

from omnimarket.inference.local_byok_credential_adapter import (
    resolve_local_byok_credential_model,
    resolve_local_byok_credential_models,
)
from omnimarket.inference.task_class_authority import load_task_class_authority
from omnimarket.nodes.node_local_secret_store_effect.handlers.handler_local_secret_store import (
    HandlerLocalSecretStore,
)
from omnimarket.nodes.node_local_secret_store_effect.models.model_local_secret_request import (
    ModelLocalSecretRequest,
)
from omnimarket.routing.byok_provider_backends import (
    ByokCatalogError,
    ModelByokRecommendedOrder,
    _read_recommended_order,
    load_byok_local_recommendation,
    load_byok_recommended_order,
    recommended_local_models,
    recommended_models,
    resolve_byok_backend_by_id,
    resolve_byok_provider_backend,
)
from omnimarket.routing.delegation_backend_resolution import (
    ModelResolvedDelegationBackend,
)
from omnimarket.routing.local_byok_route import next_chosen_model_byok_route
from tests.chains.local.harness import use_local_store

pytestmark = pytest.mark.unit


def _served_classes() -> list[str]:
    authority = load_task_class_authority()
    served = authority.universe - set(authority.unroutable_task_classes)
    return sorted(served)


def test_ac1_every_served_class_resolves_a_recommended_order() -> None:
    classes = _served_classes()
    assert classes
    for work_class in classes:
        for provider in ("openrouter", "gemini", "openai", "anthropic"):
            models = recommended_models(provider, work_class=work_class)
            assert models
            assert len(models) == len(set(models))
        assert recommended_local_models(32, work_class=work_class)


def test_ac1_a_class_missing_from_the_contract_is_refused() -> None:
    with pytest.raises(ByokCatalogError):
        recommended_models("openai", work_class="no_such_class")


def test_ac1_a_class_naming_no_declared_order_is_refused(tmp_path: Path) -> None:
    undeclared = tmp_path / "undeclared_order.yaml"
    undeclared.write_text(
        "recommended_order:\n"
        "  onboarding: baseline\n"
        "  orders:\n"
        "    baseline:\n"
        "      models: default_models\n"
        '      basis: "OMN-21215"\n'
        "  classes:\n"
        "    test: missing\n",
        encoding="utf-8",
    )
    with pytest.raises(ByokCatalogError):
        _read_recommended_order(undeclared)

    absent = tmp_path / "absent_order.yaml"
    absent.write_text("universe: []\n", encoding="utf-8")
    with pytest.raises(ByokCatalogError):
        _read_recommended_order(absent)


def test_ac1_the_onboarding_order_is_declared() -> None:
    block: ModelByokRecommendedOrder = load_byok_recommended_order()
    assert block.onboarding in block.orders
    openrouter = resolve_byok_provider_backend("openrouter")
    assert openrouter is not None
    assert recommended_models("openrouter") == openrouter.default_models


def test_ac1_ollama_order_is_the_model_sized_to_the_mac() -> None:
    by_floor = {
        entry.min_memory_gb: entry.model
        for entry in load_byok_local_recommendation().memory_classes
    }
    assert recommended_local_models(32) == (by_floor[32],)
    assert recommended_local_models(128) == (by_floor[64],)
    assert recommended_local_models(8) == ()


@pytest.mark.parametrize(
    "backend_id", ["byok-openrouter", "byok-gemini", "byok-openai"]
)
def test_ac3_accepting_the_recommendation_is_the_plan_the_route_walks(
    backend_id: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = use_local_store(monkeypatch, tmp_path)
    row = resolve_byok_backend_by_id(backend_id)
    assert row is not None
    recommended: tuple[str, ...] = recommended_models(row.provider)
    HandlerLocalSecretStore().handle(
        ModelLocalSecretRequest(
            operation="set",
            secret_ref=f"llm.{row.provider}.api_key",
            value=SecretStr(f"synthetic-omn21209-{row.provider}-key"),
            plan=row.plan,
            models=recommended,
            force=True,
        )
    )
    assert (
        resolve_local_byok_credential_models(row.provider, db_path=db_path)
        == recommended
    )

    route: ModelResolvedDelegationBackend = ModelResolvedDelegationBackend(
        backend_id=row.backend_id,
        model_id=resolve_local_byok_credential_model(row.provider, db_path=db_path)
        or "",
        endpoint_ref=row.endpoint_url,
        tier="cheap_cloud",
        max_tokens=256,
        timeout_ms=60000,
    )
    plan: list[str] = [route.model_id]
    tried: frozenset[tuple[str, str]] = frozenset()
    nxt: ModelResolvedDelegationBackend | None
    while (
        nxt := next_chosen_model_byok_route(route, tried_models=tried, db_path=db_path)
    ) is not None:
        tried = tried | {(route.backend_id, route.model_id)}
        assert nxt.backend_id == row.backend_id
        plan.append(nxt.model_id)
        route = nxt
    assert plan == list(recommended)


def test_ac4_every_served_class_names_its_basis() -> None:
    block: ModelByokRecommendedOrder = load_byok_recommended_order()
    for work_class in _served_classes():
        order = block.orders[block.classes[work_class]]
        assert order.basis
        assert re.fullmatch(r"^(OMN-[0-9]+|RULING .+Z)$", order.basis)


def test_ac4_an_unsourced_order_is_refused(tmp_path: Path) -> None:
    empty_basis = tmp_path / "empty_basis.yaml"
    empty_basis.write_text(
        "recommended_order:\n"
        "  onboarding: baseline\n"
        "  orders:\n"
        "    baseline:\n"
        "      models: default_models\n"
        '      basis: ""\n'
        "  classes:\n"
        "    test: baseline\n",
        encoding="utf-8",
    )
    with pytest.raises(ByokCatalogError):
        _read_recommended_order(empty_basis)

    missing_basis = tmp_path / "missing_basis.yaml"
    missing_basis.write_text(
        "recommended_order:\n"
        "  onboarding: baseline\n"
        "  orders:\n"
        "    baseline:\n"
        "      models: default_models\n"
        "  classes:\n"
        "    test: baseline\n",
        encoding="utf-8",
    )
    with pytest.raises(ByokCatalogError):
        _read_recommended_order(missing_basis)


@pytest.mark.parametrize("provider", ["gemini", "openrouter", "openai"])
def test_ac2_onboarding_has_a_preselected_order_for_each_key_it_offers(
    provider: str,
) -> None:
    """Onboarding offers these three keys; each has the order it pre-selects."""
    models = recommended_models(provider)
    assert models
    assert len(models) == len(set(models))


@pytest.mark.parametrize("memory_gb", [16, 32, 64])
def test_ac2_onboarding_has_a_preselected_ollama_model_for_each_memory_class(
    memory_gb: int,
) -> None:
    assert len(recommended_local_models(memory_gb)) == 1
