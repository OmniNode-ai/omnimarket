# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-integrity coverage for cloud delegation routing (OMN-15503).

The delegate-skill contract owns the public 13-class task taxonomy.  Every
declared tier in each class's closed escalation policy must be structurally
routable when its backend is available, and a tier name must never disguise a
retry of an already-failed backend as independent fallback capacity.

Failure-domain checks are intentionally limited to what the committed
contracts can prove: a complete endpoint origin plus the logical credential
reference.  Runtime quota state is not inferred here.  The routing wire can
exclude concrete backend refs, so two distinct refs sharing one provider and
credential are rejected as false fallback diversity; recurrence of the exact
same ref is permitted only because backend exclusions make it unreachable
after its first transport failure.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

import pytest
import yaml

from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
    handler_delegation_routing as routing,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_delegation_config import (
    ModelDelegationConfig,
    parse_delegation_config_yaml,
)

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_DELEGATE_CONTRACT_PATH = (
    _PROJECT_ROOT
    / "src/omnimarket/nodes/node_delegate_skill_orchestrator/contract.yaml"
)
_ROUTING_TIERS_PATH = _PROJECT_ROOT / "src/omnimarket/configs/routing_tiers.yaml"
_TASK_CONTRACT_PATH = (
    _PROJECT_ROOT / "src/omnimarket/configs/task_class_contracts.v1.yaml"
)
_BIFROST_PATH = _PROJECT_ROOT / "src/omnimarket/configs/bifrost_delegation.yaml"


def _yaml_mapping(path: Path) -> dict[str, object]:
    raw = yaml.safe_load(path.read_text())
    assert isinstance(raw, dict), f"{path} must contain a YAML mapping"
    return raw


def _allowed_task_types() -> tuple[str, ...]:
    raw = _yaml_mapping(_DELEGATE_CONTRACT_PATH).get("allowed_task_types")
    assert isinstance(raw, list)
    assert all(isinstance(item, str) for item in raw)
    return tuple(raw)


def _routing_config() -> ModelDelegationConfig:
    return parse_delegation_config_yaml(_ROUTING_TIERS_PATH.read_text())


def _synthetic_available_backends(
    config: ModelDelegationConfig,
) -> dict[str, routing.BifrostBackendRef]:
    """Make endpoint/secret availability deterministic for structural tests."""
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
                    max_tokens=model.max_context_tokens,
                ),
            )
    return backends


def _selected_backend_refs_by_task(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, tuple[str, ...]]:
    config = _routing_config()
    contract = _yaml_mapping(_TASK_CONTRACT_PATH)
    backends = _synthetic_available_backends(config)
    monkeypatch.setattr(routing, "_backend_secret_available", lambda _backend: True)

    selected_by_task: dict[str, tuple[str, ...]] = {}
    for task_type in _allowed_task_types():
        entry = routing._task_class_entry(contract, task_type)
        assert entry is not None
        contract_model_ref = routing._get_contract_model_ref(
            task_type, contract=contract
        )
        selected: list[str] = []
        for tier in routing._tier_order_from_contract(config, entry):
            model = routing._select_model_for_task(
                tier.models,
                task_type,
                0,
                backends,
                contract_model_ref=contract_model_ref,
            )
            if model is not None:
                selected.append(model.backend_ref)
        selected_by_task[task_type] = tuple(selected)
    return selected_by_task


@pytest.mark.unit
def test_delegate_contract_owns_exactly_fifteen_declared_task_classes() -> None:
    allowed = _allowed_task_types()
    task_classes = _yaml_mapping(_TASK_CONTRACT_PATH).get("task_classes")

    assert len(allowed) == 15
    assert len(set(allowed)) == 15
    assert isinstance(task_classes, dict)
    assert set(allowed).issubset(task_classes)


@pytest.mark.unit
def test_prompt_subset_uses_generic_fallback_only_for_new_internal_classes() -> None:
    missing = sorted(set(_allowed_task_types()) - routing._SYSTEM_PROMPTS.keys())
    assert missing == ["documentation", "validator_generation"]


@pytest.mark.unit
def test_every_declared_tier_is_structurally_routable_for_all_fifteen_classes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A closed tier_order cannot contain decorative, unreachable tiers."""
    monkeypatch.delenv("ONEX_DELEGATION_ALLOW_PAID", raising=False)
    monkeypatch.setattr(routing, "_backend_secret_available", lambda _backend: True)
    config = _routing_config()
    contract = _yaml_mapping(_TASK_CONTRACT_PATH)
    backends = _synthetic_available_backends(config)
    unroutable: dict[str, list[str]] = {}

    for task_type in _allowed_task_types():
        entry = routing._task_class_entry(contract, task_type)
        assert entry is not None
        tiers = routing._tier_order_from_contract(config, entry)
        assert tiers, f"{task_type} has an empty escalation tier_order"
        missing = [
            tier.name
            for tier in tiers
            if not routing._tier_can_route_task(
                tier,
                task_type,
                backends,
                contract,
            )
        ]
        if missing:
            unroutable[task_type] = missing

    assert unroutable == {}, f"declared tiers without task capacity: {unroutable}"


@pytest.mark.unit
def test_repaired_task_classes_are_explicit_capabilities_on_every_declared_tier() -> (
    None
):
    """Do not rely on the default-model off-capability escape hatch for these gaps."""
    config = _routing_config()
    contract = _yaml_mapping(_TASK_CONTRACT_PATH)
    missing: dict[str, list[str]] = {}

    for task_type in ("planning", "review", "agent_delegation"):
        entry = routing._task_class_entry(contract, task_type)
        assert entry is not None
        absent = [
            tier.name
            for tier in routing._tier_order_from_contract(config, entry)
            if not any(task_type in model.use_for for model in tier.models)
        ]
        if absent:
            missing[task_type] = absent

    assert missing == {}, f"task capability absent from declared tiers: {missing}"


@pytest.mark.unit
def test_agent_delegation_selects_the_declared_local_reasoner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default Qwen id must not silently send orchestration to local-coder."""
    selected = _selected_backend_refs_by_task(monkeypatch)
    assert selected["agent_delegation"][0] == "local-reasoner"


def _failure_domains() -> dict[str, tuple[str, str]]:
    raw_backends = _yaml_mapping(_BIFROST_PATH).get("backends")
    assert isinstance(raw_backends, list)
    domains: dict[str, tuple[str, str]] = {}
    for raw in raw_backends:
        assert isinstance(raw, dict)
        backend_id = raw.get("backend_id")
        endpoint_url = raw.get("endpoint_url")
        credential = raw.get("secret_ref") or raw.get("api_key_env")
        if not isinstance(backend_id, str) or not isinstance(endpoint_url, str):
            # Overlay-dependent/local endpoints have no statically provable origin.
            continue
        parsed = urlsplit(endpoint_url)
        if not parsed.scheme or not parsed.netloc:
            continue
        credential_ref = (
            credential if isinstance(credential, str) else "unauthenticated"
        )
        domains[backend_id] = (
            f"{parsed.scheme.lower()}://{parsed.netloc.lower()}",
            credential_ref,
        )
    return domains


@pytest.mark.unit
def test_distinct_backend_refs_do_not_fake_distinct_failure_domains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Different refs on one endpoint+credential cannot count as a fallback."""
    selected_by_task = _selected_backend_refs_by_task(monkeypatch)
    domains = _failure_domains()
    false_diversity: list[tuple[str, str, str]] = []
    exact_ref_recurrence: list[tuple[str, str]] = []

    for task_type, backend_refs in selected_by_task.items():
        seen: dict[tuple[str, str], str] = {}
        for backend_ref in backend_refs:
            domain = domains.get(backend_ref)
            if domain is None:
                continue
            previous_ref = seen.get(domain)
            if previous_ref is not None:
                if previous_ref == backend_ref:
                    exact_ref_recurrence.append((task_type, backend_ref))
                else:
                    false_diversity.append((task_type, previous_ref, backend_ref))
            seen[domain] = backend_ref

    assert false_diversity == []
    assert exact_ref_recurrence, (
        "fixture must exercise at least one repeated backend ref across tiers"
    )


@pytest.mark.unit
def test_next_tier_skips_a_repeated_exhausted_backend_ref(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The claude tier is not new quota when it repeats cloud-gemini-pro."""
    config = _routing_config()
    contract = _yaml_mapping(_TASK_CONTRACT_PATH)
    backends = _synthetic_available_backends(config)
    monkeypatch.setattr(routing, "_get_config", lambda: config)
    monkeypatch.setattr(routing, "_get_task_class_contract", lambda: contract)
    monkeypatch.setattr(routing, "_load_bifrost_endpoints", lambda: backends)
    monkeypatch.setattr(routing, "_backend_secret_available", lambda _backend: True)

    assert (
        routing.next_eligible_tier(
            "cheap_cloud",
            frozenset(),
            task_type="research",
            excluded_backend_refs=frozenset({"cloud-gemini-pro"}),
        )
        is None
    )
