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
def test_every_declared_task_class_has_a_dedicated_system_prompt() -> None:
    """No allowed task class may silently fall back to the generic prompt.

    OMN-15651: the 13->15 allowed_task_types expansion (omnimarket#2012)
    admitted `documentation` and `validator_generation` without adding
    matching `_SYSTEM_PROMPTS` entries, so both silently hit the generic
    fallback at the model-selection call site. This test restores the
    universal invariant — every declared class owns a dedicated prompt.
    """
    missing = sorted(set(_allowed_task_types()) - routing._SYSTEM_PROMPTS.keys())
    assert missing == []


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

    # OMN-15961: agent_delegation is a KNOWN, NAMED exception, not a silent
    # gate weakening. Its task class requires agent_orchestration
    # (task_class_contracts.v1.yaml), a capability no tier in this
    # HTTP-completion-only file can genuinely provide — routing_tiers.yaml
    # previously papered over the gap with a false use_for claim (OMN-15503),
    # which this ticket removed. agent_delegation stays in its declared
    # tier_order (WS-4/C6 will make the tiers real, not remove them from the
    # order) but is correctly unroutable on all three until that lands.
    #
    # OMN-16811: the exception is no longer written out here. It is read from
    # the task-class contract's own ``routing_availability`` declaration, so a
    # class can only be excused by a machine-readable statement the dashboard
    # and gateway read too — not by an edit to this dict. The admissibility of
    # such a declaration (genuinely unserveable capability, cited follow-on) is
    # enforced in ``test_tier_endpoint_completeness_omn16811``.
    known_unroutable_pending_agent_wiring = {
        task_type: [tier.name for tier in routing._tier_order_from_contract(config, e)]
        for task_type, e in (
            (task_type, routing._task_class_entry(contract, task_type))
            for task_type in _allowed_task_types()
        )
        if isinstance(e, dict)
        and isinstance(e.get("routing_availability"), dict)
        and e["routing_availability"].get("status") == "pending_capability"  # type: ignore[union-attr]
    }
    assert known_unroutable_pending_agent_wiring, (
        "the contract must still declare the pending agent_orchestration gap"
    )
    assert unroutable == known_unroutable_pending_agent_wiring, (
        f"declared tiers without task capacity: {unroutable}"
    )


@pytest.mark.unit
def test_repaired_task_classes_are_explicit_capabilities_on_every_declared_tier() -> (
    None
):
    """Do not rely on the default-model off-capability escape hatch for these gaps.

    OMN-15961: ``agent_delegation`` REMOVED from this tuple. It is no longer
    an explicit capability on any declared tier by design (see
    ``test_no_use_for_entry_claims_a_capability_its_tier_cannot_serve`` above
    and ``test_every_declared_tier_is_structurally_routable_for_all_fifteen_classes``'s
    named exception) — declaring it here would re-assert the false claim this
    ticket removed.
    """
    config = _routing_config()
    contract = _yaml_mapping(_TASK_CONTRACT_PATH)
    missing: dict[str, list[str]] = {}

    for task_type in ("planning", "review"):
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
def test_agent_delegation_selects_no_backend_anywhere(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OMN-15961: agent_delegation resolves to NO backend on any declared tier.

    Superseded by OMN-15961's truth-in-naming fix: this test previously
    asserted the default Qwen id sent agent_delegation to local-reasoner
    (avoiding a silent bind to code-only local-coder) — a real selection, but
    one that let a plain HTTP text-completion model masquerade as
    ``agent_orchestration``. That capability claim is now removed from both
    ``routing_tiers.yaml`` and ``task_model_overrides``, so unpinned
    agent_delegation routing correctly finds nothing until the real
    coding-agent producer is wired (WS-4/C6).
    """
    selected = _selected_backend_refs_by_task(monkeypatch)
    assert selected["agent_delegation"] == ()


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


# OMN-15961: routing_tiers.yaml's own header states every tier "executes through
# the canonical HTTP inference path ... identical to the lower tiers" and that
# the former shelled ``cli_agents`` tier "was REMOVED" (OMN-13215) — no tier in
# this file can genuinely satisfy a task class whose ``required_capabilities``
# includes ``agent_orchestration`` (real agentic/tool-executing capability, not
# a chat-completion capability). ``agent_delegation`` was the one class that
# claimed to be served anyway (a stale artifact of the cli_agents removal that
# OMN-15503 papered over with an HTTP-completion default instead of restoring
# real agentic capability), which this ticket strips. The check below is
# capability-driven, not name-hardcoded, so it also catches a FUTURE task class
# that declares agent_orchestration and gets mistakenly wired onto this
# HTTP-only ladder.
_CAPABILITIES_NO_HTTP_TIER_CAN_SERVE: frozenset[str] = frozenset(
    {"agent_orchestration"}
)


@pytest.mark.unit
def test_no_use_for_entry_claims_a_capability_its_tier_cannot_serve() -> None:
    """No routing_tiers.yaml ``use_for`` entry may claim a task class whose
    declared ``required_capabilities`` this HTTP-completion-only file can never
    satisfy (OMN-15961 truth-in-naming; OMN-13251 rework precondition).

    Every tier in ``routing_tiers.yaml`` is a plain HTTP chat-completion
    backend (see the file's own module header, OMN-13215). Declaring a
    ``use_for`` entry for a task class that requires ``agent_orchestration`` is
    a false capability claim: the tier will accept the request and return a
    text completion, not perform any agentic/tool-executing work. This is a
    mechanism, not a name-specific regression test — it fails for ANY task
    class with an unsatisfiable required capability, not only
    ``agent_delegation``.
    """
    config = _routing_config()
    contract = _yaml_mapping(_TASK_CONTRACT_PATH)
    task_classes = contract.get("task_classes")
    assert isinstance(task_classes, dict)

    violations: list[tuple[str, str, str]] = []
    for tier in config.tiers:
        for model in tier.models:
            for task_type in model.use_for:
                entry = task_classes.get(task_type)
                required = (
                    entry.get("required_capabilities", [])
                    if isinstance(entry, dict)
                    else []
                )
                unsatisfiable = _CAPABILITIES_NO_HTTP_TIER_CAN_SERVE.intersection(
                    required
                )
                if unsatisfiable:
                    violations.append((tier.name, model.backend_ref, task_type))

    assert violations == [], (
        "routing_tiers.yaml use_for claims a capability no HTTP-completion "
        f"tier can serve: {violations}"
    )


@pytest.mark.unit
def test_no_task_model_override_restores_an_agent_orchestration_class_via_id_match() -> (
    None
):
    """``task_model_overrides`` cannot silently restore the false claim the
    ``use_for`` check above forbids (OMN-15961).

    ``_select_model_for_task``'s id-match escape hatch resolves an EXPLICIT
    ``task_model_overrides`` entry to its named model regardless of that
    model's ``use_for`` list (OMN-10942/OMN-13140, by design — a legitimate
    mechanism for other task classes). That means an override entry for a
    task class requiring ``agent_orchestration`` would keep resolving to a
    plain HTTP model even with the class stripped from every tier's
    ``use_for``, defeating the fix above through a second mechanism in a
    different file. No override may name a model for such a class.
    """
    contract = _yaml_mapping(_TASK_CONTRACT_PATH)
    task_classes = contract.get("task_classes")
    assert isinstance(task_classes, dict)
    overrides = contract.get("task_model_overrides")
    assert isinstance(overrides, dict)

    violations: list[tuple[str, str]] = []
    for task_type, model_id in overrides.items():
        entry = task_classes.get(task_type)
        required = (
            entry.get("required_capabilities", []) if isinstance(entry, dict) else []
        )
        if _CAPABILITIES_NO_HTTP_TIER_CAN_SERVE.intersection(required):
            violations.append((task_type, str(model_id)))

    assert violations == [], (
        "task_model_overrides pins a capability no HTTP-completion tier can "
        f"serve: {violations}"
    )
