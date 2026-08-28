# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tier-endpoint completeness for every admitted task class (OMN-16811).

The 13-class delegation matrix on the dev lane returned ``dispatch_timeout``
for ``agent_delegation`` and ``escalation`` while the other eleven classes
terminated inside the ingress budget. A read-only probe run inside the dev
runtime container (``omninode-runtime``, 2026-08-28) resolved the two
symptoms to DIFFERENT causes, and this module pins both:

* ``agent_delegation`` selects no backend on any of its three declared tiers,
  so the routing handler raises ``ProtocolConfigurationError``
  (``ONEX_CORE_041_INVALID_CONFIGURATION``). That is the OMN-15961 fail-closed
  decision, still enforced by
  ``test_no_use_for_entry_claims_a_capability_its_tier_cannot_serve``: no
  HTTP-completion tier can satisfy ``agent_orchestration``. The defect is not
  that routing fails — it is that the gap was known only to a test-local
  exception dict, so the product surface kept offering the class. It is now
  DECLARED in the task-class contract itself (``routing_availability``), which
  is a machine-readable fact any consumer (dashboard, gateway) can read.

* ``escalation`` DOES resolve — but to exactly one rung, ``claude`` →
  ``cloud-gemini-pro``. It is the only admitted class whose whole ladder is a
  single credentialed cloud backend, so a quota-exhausted or unreachable
  Gemini strands the class with no local fallback while every other class
  keeps routing on the owned GPUs. The cheapest-first ladder doctrine says the
  free local tier is tried first; this module pins that ordering AND the
  no-single-credentialed-failure-domain invariant that generalizes the defect.

Config-resolution level only. Live dispatch behavior is deploy-gated and is
not asserted here.
"""

from __future__ import annotations

from pathlib import Path

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

# Capabilities no plain HTTP chat-completion tier can provide. Mirrors
# ``test_cloud_routing_contract_integrity_omn15503``'s set (OMN-15961): a
# ``routing_availability`` declaration is only admissible when the missing
# capability is genuinely unserveable, never as a way to silence a routable
# class that someone forgot to wire.
_UNSERVEABLE_CAPABILITIES: frozenset[str] = frozenset({"agent_orchestration"})


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


def _credentialed_backend_refs() -> frozenset[str]:
    """Backend ids that need a resolvable credential to be usable.

    A backend declaring ``secret_ref`` or ``api_key_env`` is only routable when
    that credential resolves on the lane AND the provider still has quota. The
    local backends declare neither: they run on owned GPUs.
    """
    raw_backends = _yaml_mapping(_BIFROST_PATH).get("backends")
    assert isinstance(raw_backends, list)
    credentialed: set[str] = set()
    for raw in raw_backends:
        assert isinstance(raw, dict)
        backend_id = raw.get("backend_id")
        if not isinstance(backend_id, str):
            continue
        if raw.get("secret_ref") or raw.get("api_key_env"):
            credentialed.add(backend_id)
    assert credentialed, "fixture must find at least one credentialed backend"
    return frozenset(credentialed)


def _available_backends(
    config: ModelDelegationConfig,
    unavailable: frozenset[str] = frozenset(),
) -> dict[str, routing.BifrostBackendRef]:
    """Synthetic endpoint availability, minus the named unavailable backends."""
    backends: dict[str, routing.BifrostBackendRef] = {}
    for tier in config.tiers:
        for model in tier.models:
            if model.backend_ref in unavailable:
                continue
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


def _routable_ladder(
    task_type: str,
    *,
    unavailable: frozenset[str] = frozenset(),
) -> tuple[tuple[str, str], ...]:
    """Return ``(tier_name, backend_ref)`` for every rung that actually routes."""
    config = _routing_config()
    contract = _yaml_mapping(_TASK_CONTRACT_PATH)
    backends = _available_backends(config, unavailable)
    entry = routing._task_class_entry(contract, task_type)
    assert entry is not None, f"{task_type} has no task-class contract entry"
    contract_model_ref = routing._get_contract_model_ref(task_type, contract=contract)
    explicit = routing._is_explicit_task_model_override(task_type, contract=contract)

    ladder: list[tuple[str, str]] = []
    for tier in routing._tier_order_from_contract(config, entry):
        if not routing._tier_allowed_by_contract(tier, entry):
            continue
        model = routing._select_model_for_task(
            tier.models,
            task_type,
            0,
            backends,
            contract_model_ref=contract_model_ref,
            contract_model_ref_is_explicit_override=explicit,
        )
        if model is not None:
            ladder.append((tier.name, model.backend_ref))
    return tuple(ladder)


def _routing_availability(task_type: str) -> dict[str, object] | None:
    entry = routing._task_class_entry(_yaml_mapping(_TASK_CONTRACT_PATH), task_type)
    assert entry is not None
    declared = entry.get("routing_availability")
    if declared is None:
        return None
    assert isinstance(declared, dict), (
        f"{task_type}.routing_availability must be a mapping"
    )
    return declared


def _pending_task_types() -> frozenset[str]:
    pending: set[str] = set()
    for task_type in _allowed_task_types():
        declared = _routing_availability(task_type)
        if declared is not None and declared.get("status") == "pending_capability":
            pending.add(task_type)
    return frozenset(pending)


@pytest.fixture(autouse=True)
def _paid_tier_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Assert against the default paid posture, not an ambient opt-out."""
    monkeypatch.delenv("ONEX_DELEGATION_ALLOW_PAID", raising=False)
    monkeypatch.setattr(routing, "_backend_secret_available", lambda _backend: True)


@pytest.mark.unit
def test_escalation_routes_with_every_credentialed_backend_unavailable() -> None:
    """The escalation class must not strand when no cloud credential resolves.

    Dev-lane ground truth (read-only in-container probe, 2026-08-28): the
    escalation ladder resolved to exactly ``[('claude', 'cloud-gemini-pro')]``.
    Every other admitted class kept a local rung, which is why eleven of
    thirteen classes terminated and this one did not.
    """
    ladder = _routable_ladder("escalation", unavailable=_credentialed_backend_refs())

    assert ladder, (
        "escalation resolves no backend once credentialed cloud backends are "
        "unavailable — its whole ladder is one metered cloud rung"
    )


@pytest.mark.unit
def test_escalation_tries_a_free_tier_before_the_metered_ceiling() -> None:
    """Cheapest-first: the first routable rung of escalation costs nothing."""
    config = _routing_config()
    cost_by_tier = {tier.name: tier.cost_per_1k_tokens for tier in config.tiers}
    ladder = _routable_ladder("escalation")

    assert ladder, "escalation must resolve at least one rung"
    first_tier = ladder[0][0]
    assert cost_by_tier[first_tier] == 0.0, (
        f"escalation's first routable rung is metered tier '{first_tier}'; the "
        "ladder must try a zero-cost tier first"
    )


@pytest.mark.unit
def test_no_routable_task_class_depends_on_a_single_failure_domain() -> None:
    """Generalizes the defect: no admitted class may be credential-only.

    A class whose entire ladder is credentialed backends dies with the
    provider's quota, key, or reachability. Every routable class must keep at
    least one rung that runs without a credential (the owned-GPU local tier).
    """
    credentialed = _credentialed_backend_refs()
    pending = _pending_task_types()
    credential_only: dict[str, tuple[tuple[str, str], ...]] = {}

    for task_type in _allowed_task_types():
        if task_type in pending:
            continue
        full = _routable_ladder(task_type)
        assert full, f"{task_type} resolves no tier at all"
        if not _routable_ladder(task_type, unavailable=credentialed):
            credential_only[task_type] = full

    assert credential_only == {}, (
        f"task classes with no credential-free rung: {credential_only}"
    )


@pytest.mark.unit
def test_every_admitted_task_class_is_routable_or_declared_pending() -> None:
    """AC3 drift gate — the two surfaces cannot diverge silently again.

    Fails in BOTH directions: an admitted class that resolves no tier without a
    ``routing_availability`` declaration, and a stale declaration left on a
    class that has since become routable.
    """
    unroutable = {
        task_type
        for task_type in _allowed_task_types()
        if not _routable_ladder(task_type)
    }
    pending = _pending_task_types()

    assert unroutable == pending, (
        "admitted task classes must be routable or contract-declared pending; "
        f"unroutable={sorted(unroutable)} declared_pending={sorted(pending)}"
    )


@pytest.mark.unit
def test_pending_declarations_name_a_genuinely_unserveable_capability() -> None:
    """A pending declaration is a capability statement, not a mute button."""
    task_classes = _yaml_mapping(_TASK_CONTRACT_PATH).get("task_classes")
    assert isinstance(task_classes, dict)

    for task_type in sorted(_pending_task_types()):
        declared = _routing_availability(task_type)
        assert declared is not None
        missing = declared.get("missing_capability")
        assert isinstance(missing, str), (
            f"{task_type}.routing_availability must name missing_capability"
        )
        assert missing, f"{task_type}.routing_availability.missing_capability is empty"
        assert missing in _UNSERVEABLE_CAPABILITIES, (
            f"{task_type} declares missing_capability='{missing}', which is not "
            "in the set of capabilities no HTTP-completion tier can serve — a "
            "routable class cannot be declared pending"
        )
        entry = task_classes.get(task_type)
        assert isinstance(entry, dict)
        required = entry.get("required_capabilities")
        assert isinstance(required, list), (
            f"{task_type} must declare required_capabilities as a list"
        )
        assert missing in required, (
            f"{task_type}.routing_availability.missing_capability must be one "
            "of the class's own required_capabilities"
        )
        tracking = declared.get("tracking")
        assert isinstance(tracking, str), (
            f"{task_type}.routing_availability must cite the follow-on ticket"
        )
        assert tracking.strip(), f"{task_type}.routing_availability.tracking is empty"


@pytest.mark.unit
def test_agent_delegation_is_the_declared_pending_class() -> None:
    """Pin the known gap so removing the declaration cannot pass unnoticed."""
    assert _pending_task_types() == frozenset({"agent_delegation"})
    assert _routable_ladder("agent_delegation") == ()
