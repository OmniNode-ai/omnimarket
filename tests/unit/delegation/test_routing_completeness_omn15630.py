# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Routing-completeness invariant (OMN-15630).

Residual R4-b of OMN-15623's round-3 adversarial check: 4 of 15 task classes
declared in ``task_class_contracts.v1.yaml`` had an unserved rung in their
OWN ``escalation_policy.tier_order`` — ``documentation``/``validator_generation``
were in NO tier's ``use_for`` anywhere and silently bound the code-only
``local-coder`` backend via the ``default_task_model_ref`` implicit pin
(a recurrence, in kind, of OMN-14104).

RED-before, exact (``omnimarket@58de0731``, reproduced live by this ticket's
implementer via a throwaway probe script executing ``_select_model_for_task``
with ``contract_model_ref=None`` over the real config files — not re-derived
from the ticket body):

    refactor              -> claude
    documentation         -> local, cheap_cloud
    validator_generation  -> local, cheap_cloud, claude
    summarization         -> local

The naive AC framing ("served by >=1 backend in >=1 tier OR carries an
explicit contract_model_ref pin") is VACUOUSLY GREEN at that commit:
``default_task_model_ref: Qwen3.6-35B-A3B`` means every one of the 15 classes
"carries a pin." ``test_every_declared_class_is_served_at_every_declared_tier``
below evaluates through the REAL ``_select_model_for_task`` with
``contract_model_ref=None`` explicitly, so that implicit default pin cannot
mask a gap — matching OMN-15630 AC1 exactly.

GREEN-after this PR: routing_tiers.yaml gained ``use_for`` entries closing
all 4 gaps (AC3 — content, not deletion or a silencing override). AC4's
silent wrong-capability bind is CLOSED (not merely justified): the
``id_matches[0]`` off-``use_for`` escape hatch in ``_select_model_for_task``
now only fires for an EXPLICIT ``task_model_overrides`` pin, never the
IMPLICIT ``default_task_model_ref`` fallback — see
``TestImplicitDefaultPinCannotOverrideCapability`` below, which drives the
actual seam (task_type not in any use_for + implicit default pin active) and
proves the existing OMN-10942/OMN-13140 explicit-pin carve-out is unbroken.
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
from omnimarket.nodes.node_delegation_routing_reducer.models.model_routing_tier import (
    ModelRoutingTier,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_tier_model import (
    ModelTierModel,
)
from tests.constants import MODEL_QWEN3_35B_A3B

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_ROUTING_TIERS_PATH = _PROJECT_ROOT / "src/omnimarket/configs/routing_tiers.yaml"
_TASK_CONTRACT_PATH = (
    _PROJECT_ROOT / "src/omnimarket/configs/task_class_contracts.v1.yaml"
)

# The exact RED-before set (OMN-15630 AC2), retained here so a regression that
# reopens one of these gaps is traceable back to the ticket that closed it.
_RED_BEFORE_UNSERVED_RUNGS: dict[str, tuple[str, ...]] = {
    "refactor": ("claude",),
    "documentation": ("local", "cheap_cloud"),
    "validator_generation": ("local", "cheap_cloud", "claude"),
    "summarization": ("local",),
}


def _yaml_mapping(path: Path) -> dict[str, object]:
    raw = yaml.safe_load(path.read_text())
    assert isinstance(raw, dict), f"{path} must contain a YAML mapping"
    return raw


def _routing_config() -> ModelDelegationConfig:
    return parse_delegation_config_yaml(_ROUTING_TIERS_PATH.read_text())


def _synthetic_available_backends(
    config: ModelDelegationConfig,
) -> dict[str, routing.BifrostBackendRef]:
    """Deterministic endpoint/secret availability for structural tests.

    Mirrors the fixture shape in test_cloud_routing_contract_integrity_omn15503.py
    (OMN-15503) — synthesized so this test proves ``use_for`` coverage, not live
    provider/network/credential state (OMN-15630 AC1: "no provider, network, or
    credential dependency").
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
                    max_tokens=model.max_context_tokens,
                ),
            )
    return backends


def _unserved_rungs_by_class(
    config: ModelDelegationConfig,
    contract: dict[str, object],
    backends: dict[str, routing.BifrostBackendRef],
) -> dict[str, list[str]]:
    """For every declared task class, list tier_order rungs with zero server.

    Pure — no I/O, no monkeypatching. Evaluates through the REAL
    ``_select_model_for_task`` with ``contract_model_ref=None`` so a
    ``default_task_model_ref`` implicit pin can never mask a gap (OMN-15630
    AC1). Reused by both the real-repo test and the synthetic negative test
    below so the checker's sensitivity is proven, not merely asserted
    (memory: prove RED against exists-but-wrong, not merely absent).
    """
    task_classes = contract.get("task_classes")
    assert isinstance(task_classes, dict)

    unserved: dict[str, list[str]] = {}
    for task_type, entry in task_classes.items():
        assert isinstance(entry, dict)
        tiers = routing._tier_order_from_contract(config, entry)
        assert tiers, f"{task_type} declares an empty escalation tier_order"
        missing = [
            tier.name
            for tier in tiers
            if routing._select_model_for_task(
                tier.models,
                task_type,
                0,
                backends,
                contract_model_ref=None,
            )
            is None
        ]
        if missing:
            unserved[task_type] = missing
    return unserved


# ---------------------------------------------------------------------------
# AC1/AC2/AC3 — routing completeness over the REAL config files
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_task_class_contract_declares_at_least_fifteen_classes() -> None:
    """Locks the RED-before class population as a FLOOR, not a ceiling
    (OMN-15630 AC2; remediation round 1 renamed this from the prior
    ``..._declares_exactly_fifteen_classes`` once the assertion itself
    loosened from ``== 15`` to ``>= 15`` — the old name asserted something
    the body no longer checked).

    The config's declared population — not OMN-15503's stale 13-class
    ``allowed_task_types`` subset, which OMN-15623 C5 recorded as never having
    been the config population. A legitimate 16th+ declared class is caught
    by ``test_every_declared_class_is_served_at_every_declared_tier_unpinned``
    below (coverage), not by this population count.
    """
    contract = _yaml_mapping(_TASK_CONTRACT_PATH)
    task_classes = contract.get("task_classes")
    assert isinstance(task_classes, dict)
    # Floor, not a ceiling (remediation of OMN-15630 round 1): a legitimate
    # 16th+ class must fail on coverage via
    # test_every_declared_class_is_served_at_every_declared_tier_unpinned,
    # not on this population count. A `==` lock invited bumping the literal
    # instead of serving the new class.
    assert len(task_classes) >= 15
    assert set(_RED_BEFORE_UNSERVED_RUNGS).issubset(task_classes)


@pytest.mark.unit
def test_every_declared_class_is_served_at_every_declared_tier_unpinned() -> None:
    """OMN-15630 AC1/AC2/AC3 — GREEN after content repair.

    Evaluated with ``contract_model_ref=None`` (via ``_unserved_rungs_by_class``
    -> the real ``_select_model_for_task``) so the ``default_task_model_ref``
    implicit pin cannot manufacture a false green. A check that instead relied
    on ``_tier_can_route_task`` (which resolves the pin) would pass vacuously
    today exactly as it did before this PR's content fix — see
    test_repaired_task_classes_are_explicit_capabilities_on_every_declared_tier
    below for that mechanism proven directly.

    OMN-15961: ``agent_delegation`` is a KNOWN, NAMED exception below, not a
    silent gate weakening. It requires ``agent_orchestration``
    (task_class_contracts.v1.yaml), a capability no tier in this
    HTTP-completion-only file can genuinely provide; the prior use_for/
    task_model_overrides entries that made it look "served" were a false
    capability claim (OMN-15503) that ticket removed. Unserved on every rung
    is the correct, honest state until the real coding-agent producer is
    wired (WS-4/C6).
    """
    config = _routing_config()
    contract = _yaml_mapping(_TASK_CONTRACT_PATH)
    backends = _synthetic_available_backends(config)

    unserved = _unserved_rungs_by_class(config, contract, backends)

    known_unserved_pending_agent_wiring = {
        "agent_delegation": ["local", "cheap_cloud", "claude"],
    }
    assert unserved == known_unserved_pending_agent_wiring, (
        f"declared task classes with unserved tiers: {unserved}"
    )


@pytest.mark.unit
def test_repaired_task_classes_are_explicit_capabilities_on_every_declared_tier() -> (
    None
):
    """The four repaired classes do not rely on the default-model escape hatch.

    Mirrors OMN-15503's ``test_repaired_task_classes_are_explicit_capabilities_
    on_every_declared_tier`` (planning/review/agent_delegation) for this
    ticket's four classes: every rung of each class's own ``tier_order`` must
    carry an EXPLICIT ``use_for`` entry, not merely resolve through a pinned
    id-match.
    """
    config = _routing_config()
    contract = _yaml_mapping(_TASK_CONTRACT_PATH)
    missing: dict[str, list[str]] = {}

    for task_type in _RED_BEFORE_UNSERVED_RUNGS:
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
def test_ceiling_tier_serves_every_declared_class() -> None:
    """Every class's OWN declared ceiling (last entry of its tier_order) must
    serve that class -- mechanizes OMN-15623 C4 / OMN-15503 AC5's ceiling-
    reachability reading, generalized to all 15 classes rather than the 3
    OMN-15623 spot-checked.

    OMN-15961: ``agent_delegation``'s ``claude`` ceiling is a KNOWN, NAMED
    exception below (same rationale as
    ``test_every_declared_class_is_served_at_every_declared_tier_unpinned``
    above) -- it genuinely has no serving ceiling until the real coding-agent
    producer is wired (WS-4/C6).
    """
    config = _routing_config()
    contract = _yaml_mapping(_TASK_CONTRACT_PATH)
    backends = _synthetic_available_backends(config)
    task_classes = contract.get("task_classes")
    assert isinstance(task_classes, dict)

    dead_ceilings: dict[str, str] = {}
    for task_type, entry in task_classes.items():
        assert isinstance(entry, dict)
        tiers = routing._tier_order_from_contract(config, entry)
        ceiling = tiers[-1]
        selected = routing._select_model_for_task(
            ceiling.models,
            task_type,
            0,
            backends,
            contract_model_ref=None,
        )
        if selected is None:
            dead_ceilings[task_type] = ceiling.name

    known_dead_ceiling_pending_agent_wiring = {"agent_delegation": "claude"}
    assert dead_ceilings == known_dead_ceiling_pending_agent_wiring, (
        f"declared ceiling tier does not serve class: {dead_ceilings}"
    )


# ---------------------------------------------------------------------------
# Sensitivity proof — the checker must actually catch a gap, not just pass
# (memory: prove RED against exists-but-wrong, not merely absent)
# ---------------------------------------------------------------------------


def _synthetic_config_and_contract() -> tuple[ModelDelegationConfig, dict[str, object]]:
    """A minimal, hand-built two-tier config with a deliberate coverage gap.

    ``summarization`` declares tier_order [local, cheap_cloud]; ``local``
    carries a model that does NOT declare summarization in use_for and
    ``cheap_cloud`` does — reproducing exactly the shape of the real
    OMN-15630 defect (one served rung, one unserved rung) so the negative
    test is a faithful analogue, not a trivial always-empty-tier case.
    """
    local_tier = ModelRoutingTier(
        name="local",
        cost_per_1k_tokens=0.0,
        models=(
            ModelTierModel(
                id=MODEL_QWEN3_35B_A3B,
                backend_ref="synthetic-local-coder",
                max_context_tokens=65536,
                use_for=("code_generation",),
                fast_path_threshold_tokens=65536,
            ),
        ),
        eval_before_accept=True,
        eval_model="qwen3.6-35b",
        max_retries=2,
    )
    cheap_cloud_tier = ModelRoutingTier(
        name="cheap_cloud",
        cost_per_1k_tokens=0.002,
        models=(
            ModelTierModel(
                id="synthetic-gemini-flash",
                backend_ref="synthetic-cloud-gemini-flash",
                max_context_tokens=1_000_000,
                use_for=("summarization",),
                fast_path_threshold_tokens=None,
            ),
        ),
        eval_before_accept=True,
        eval_model="qwen3.6-35b",
        max_retries=1,
    )
    config = ModelDelegationConfig(tiers=(local_tier, cheap_cloud_tier))
    contract: dict[str, object] = {
        "version": "1.0",
        "default_task_model_ref": MODEL_QWEN3_35B_A3B,
        "task_model_overrides": {},
        "task_classes": {
            "summarization": {
                "escalation_policy": {"tier_order": ["local", "cheap_cloud"]},
            },
        },
    }
    return config, contract


@pytest.mark.unit
def test_checker_is_sensitive_to_a_synthetic_unserved_rung() -> None:
    """The invariant helper must FAIL on a config that reproduces the defect
    shape, not merely pass on the (now-repaired) real config. Proves this
    checker would have caught OMN-15630's original gap rather than being
    vacuously green regardless of input.
    """
    config, contract = _synthetic_config_and_contract()
    backends = _synthetic_available_backends(config)

    unserved = _unserved_rungs_by_class(config, contract, backends)

    assert unserved == {"summarization": ["local"]}


@pytest.mark.unit
def test_checker_is_green_once_the_synthetic_gap_is_served() -> None:
    """Companion to the sensitivity proof above: serving the gap (adding
    summarization to the local model's use_for) clears the finding — proves
    the checker is not just permanently RED on this fixture shape either.
    """
    config, contract = _synthetic_config_and_contract()
    served_local_model = ModelTierModel(
        id=MODEL_QWEN3_35B_A3B,
        backend_ref="synthetic-local-coder",
        max_context_tokens=65536,
        use_for=("code_generation", "summarization"),
        fast_path_threshold_tokens=65536,
    )
    served_local_tier = config.tiers[0].model_copy(
        update={"models": (served_local_model,)}
    )
    served_config = config.model_copy(
        update={"tiers": (served_local_tier, config.tiers[1])}
    )
    backends = _synthetic_available_backends(served_config)

    unserved = _unserved_rungs_by_class(served_config, contract, backends)

    assert unserved == {}


# ---------------------------------------------------------------------------
# AC4 — the silent wrong-capability bind (id_matches[0] fallback)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestIsExplicitTaskModelOverride:
    """``_is_explicit_task_model_override`` against the real contract."""

    def test_true_for_a_task_model_overrides_entry(self) -> None:
        contract = _yaml_mapping(_TASK_CONTRACT_PATH)
        # code_generation carries an explicit task_model_overrides entry.
        assert (
            routing._is_explicit_task_model_override("code_generation", contract)
            is True
        )

    def test_false_for_a_default_only_class(self) -> None:
        contract = _yaml_mapping(_TASK_CONTRACT_PATH)
        # documentation has no task_model_overrides entry — it falls back to
        # default_task_model_ref, the implicit case this ticket closes.
        assert (
            routing._is_explicit_task_model_override("documentation", contract) is False
        )

    def test_false_for_none_contract(self) -> None:
        assert routing._is_explicit_task_model_override("anything", None) is False


@pytest.mark.unit
class TestImplicitDefaultPinCannotOverrideCapability:
    """Drives the actual AC4 seam: task_type not in any use_for + an implicit
    default pin active -> assert the intended behavior (fall through instead
    of silently binding the off-capability id match).

    Fixture mirrors the live incident exactly: two backends in one tier share
    a model id (the OMN-14396 collision shape) — neither declares the probed
    task_type — plus a THIRD, differently-id'd backend that DOES declare it,
    proving the fallthrough reaches real capacity rather than merely
    returning None.
    """

    def _models(self) -> tuple[ModelTierModel, ...]:
        return (
            ModelTierModel(
                id=MODEL_QWEN3_35B_A3B,
                backend_ref="local-coder",
                max_context_tokens=65536,
                use_for=("code_generation", "code_review", "refactor"),
                fast_path_threshold_tokens=65536,
            ),
            ModelTierModel(
                id=MODEL_QWEN3_35B_A3B,
                backend_ref="local-heavy-reasoning",
                max_context_tokens=8192,
                use_for=("research", "reasoning"),
                fast_path_threshold_tokens=8192,
            ),
            ModelTierModel(
                id="ds-v4-flash",
                backend_ref="local-ds-v4-flash",
                max_context_tokens=65536,
                use_for=("documentation",),
                fast_path_threshold_tokens=8192,
            ),
        )

    def _backends(self) -> dict[str, routing.BifrostBackendRef]:
        return {
            "local-coder": routing.BifrostBackendRef(
                endpoint_url="https://local-coder.contract.test/v1/chat/completions",
                model_name=MODEL_QWEN3_35B_A3B,
                timeout_ms=30000,
                max_tokens=65536,
            ),
            "local-heavy-reasoning": routing.BifrostBackendRef(
                endpoint_url="https://local-heavy-reasoning.contract.test/v1/chat/completions",
                model_name=MODEL_QWEN3_35B_A3B,
                timeout_ms=30000,
                max_tokens=8192,
            ),
            "local-ds-v4-flash": routing.BifrostBackendRef(
                endpoint_url="https://local-ds-v4-flash.contract.test/v1/chat/completions",
                model_name="ds-v4-flash",
                timeout_ms=30000,
                max_tokens=65536,
            ),
        }

    def test_implicit_pin_falls_through_to_a_real_capability_match(self) -> None:
        """The bug, closed: task_type ("documentation") is absent from BOTH
        id-matching models' use_for. Before OMN-15630 this returned
        id_matches[0] (local-coder — wrong capability, silent). After: it
        falls through to the general use_for scan and finds local-ds-v4-flash,
        which actually declares "documentation"."""
        selected = routing._select_model_for_task(
            self._models(),
            "documentation",
            estimated_tokens=25,
            bifrost_backends=self._backends(),
            contract_model_ref=MODEL_QWEN3_35B_A3B,
            contract_model_ref_is_explicit_override=False,
        )

        assert selected is not None
        assert selected.backend_ref == "local-ds-v4-flash", (
            "an implicit default pin must never silently bind an id-matched "
            f"model that does not declare the task type; got {selected.backend_ref!r}"
        )

    def test_implicit_pin_returns_none_when_nothing_declares_the_task(self) -> None:
        """No model anywhere in the tier declares "planning" in this fixture —
        the implicit pin must return None, never a silent off-capability bind."""
        selected = routing._select_model_for_task(
            self._models(),
            "planning",
            estimated_tokens=25,
            bifrost_backends=self._backends(),
            contract_model_ref=MODEL_QWEN3_35B_A3B,
            contract_model_ref_is_explicit_override=False,
        )

        assert selected is None

    def test_explicit_override_carve_out_is_unbroken(self) -> None:
        """OMN-10942/OMN-13140 regression guard: an EXPLICIT task_model_overrides
        pin to an off-use_for model is still honored via id_matches[0] — only
        the IMPLICIT default fallback is restricted by this ticket. Same
        fixture, same absent task_type ("documentation"), only the
        explicit/implicit flag differs from the closed-bug test above."""
        selected = routing._select_model_for_task(
            self._models(),
            "documentation",
            estimated_tokens=25,
            bifrost_backends=self._backends(),
            contract_model_ref=MODEL_QWEN3_35B_A3B,
            contract_model_ref_is_explicit_override=True,
        )

        assert selected is not None
        assert selected.backend_ref == "local-coder", (
            "an EXPLICIT override must still resolve via the first id match "
            "(OMN-10942/OMN-13140 carve-out) even though it does not declare "
            f"the task type; got {selected.backend_ref!r}"
        )

    def test_omitted_kwarg_defaults_safe_not_the_silent_bind(self) -> None:
        """OMN-15630 remediation round 1: a caller that forgets to pass
        ``contract_model_ref_is_explicit_override`` must NOT silently
        reinstate the wrong-capability id-match escape hatch. The kwarg
        defaults to ``False`` (assume implicit unless proven explicit), so an
        omitted-kwarg call falls through to the real capability scan and
        resolves ``local-ds-v4-flash`` — same result as the explicit
        ``contract_model_ref_is_explicit_override=False`` case above, not the
        stale ``local-coder`` off-capability bind a ``True`` default would
        produce."""
        selected = routing._select_model_for_task(
            self._models(),
            "documentation",
            estimated_tokens=25,
            bifrost_backends=self._backends(),
            contract_model_ref=MODEL_QWEN3_35B_A3B,
        )

        assert selected is not None
        assert selected.backend_ref == "local-ds-v4-flash", (
            "an omitted contract_model_ref_is_explicit_override kwarg must "
            "default to the safe (implicit) path, never the id_matches[0] "
            f"escape hatch; got {selected.backend_ref!r}"
        )


@pytest.mark.unit
class TestProductionCallSitesRouteTheRepairedClassesCorrectly:
    """End-to-end (still no provider/network dependency — synthesized
    backends): the four repaired classes must resolve to a model that
    actually declares them, through the SAME call sites ``delta()``/escalation
    use, with the real (unpinned-implicit-aware) default-pin resolution."""

    def test_documentation_local_tier_resolves_a_declaring_backend(self) -> None:
        config = _routing_config()
        contract = _yaml_mapping(_TASK_CONTRACT_PATH)
        backends = _synthetic_available_backends(config)
        local_tier = next(t for t in config.tiers if t.name == "local")

        assert routing._tier_can_route_task(
            local_tier, "documentation", backends, contract
        )

        contract_model_ref = routing._get_contract_model_ref(
            "documentation", contract=contract
        )
        selected = routing._select_model_for_task(
            local_tier.models,
            "documentation",
            0,
            backends,
            contract_model_ref=contract_model_ref,
            contract_model_ref_is_explicit_override=(
                routing._is_explicit_task_model_override("documentation", contract)
            ),
        )
        assert selected is not None
        assert "documentation" in selected.use_for, (
            "documentation must resolve to a backend that actually declares "
            f"it, not a silent off-capability bind; got {selected.backend_ref!r} "
            f"(use_for={selected.use_for!r})"
        )

    def test_validator_generation_local_tier_resolves_a_declaring_backend(
        self,
    ) -> None:
        config = _routing_config()
        contract = _yaml_mapping(_TASK_CONTRACT_PATH)
        backends = _synthetic_available_backends(config)
        local_tier = next(t for t in config.tiers if t.name == "local")

        assert routing._tier_can_route_task(
            local_tier, "validator_generation", backends, contract
        )

        contract_model_ref = routing._get_contract_model_ref(
            "validator_generation", contract=contract
        )
        selected = routing._select_model_for_task(
            local_tier.models,
            "validator_generation",
            0,
            backends,
            contract_model_ref=contract_model_ref,
            contract_model_ref_is_explicit_override=(
                routing._is_explicit_task_model_override(
                    "validator_generation", contract
                )
            ),
        )
        assert selected is not None
        assert "validator_generation" in selected.use_for

    def test_summarization_local_tier_resolves_a_declaring_backend(self) -> None:
        config = _routing_config()
        contract = _yaml_mapping(_TASK_CONTRACT_PATH)
        backends = _synthetic_available_backends(config)
        local_tier = next(t for t in config.tiers if t.name == "local")

        assert routing._tier_can_route_task(
            local_tier, "summarization", backends, contract
        )

        contract_model_ref = routing._get_contract_model_ref(
            "summarization", contract=contract
        )
        selected = routing._select_model_for_task(
            local_tier.models,
            "summarization",
            0,
            backends,
            contract_model_ref=contract_model_ref,
            contract_model_ref_is_explicit_override=(
                routing._is_explicit_task_model_override("summarization", contract)
            ),
        )
        assert selected is not None
        assert "summarization" in selected.use_for

    def test_refactor_claude_ceiling_resolves_a_declaring_backend(self) -> None:
        config = _routing_config()
        contract = _yaml_mapping(_TASK_CONTRACT_PATH)
        backends = _synthetic_available_backends(config)
        claude_tier = next(t for t in config.tiers if t.name == "claude")

        assert routing._tier_can_route_task(claude_tier, "refactor", backends, contract)

        contract_model_ref = routing._get_contract_model_ref(
            "refactor", contract=contract
        )
        selected = routing._select_model_for_task(
            claude_tier.models,
            "refactor",
            0,
            backends,
            contract_model_ref=contract_model_ref,
            contract_model_ref_is_explicit_override=(
                routing._is_explicit_task_model_override("refactor", contract)
            ),
        )
        assert selected is not None
        assert "refactor" in selected.use_for


@pytest.mark.unit
class TestThreeWayModelIdCollisionDisambiguation:
    """OMN-16435 AC4: the id-match disambiguation must stay correct when a
    THIRD backend in the tier shares the pinned model id — not just the
    two-way collision ``TestImplicitDefaultPinCannotOverrideCapability``
    above already covers.

    OMN-16435's actual root cause turned out to be test-hermeticity (an
    ambient ``DELEGATION_ROUTING_TIERS_PATH``/``BIFROST_CONTRACT_PATH`` from
    a developer's real delegation-dispatch environment silently overriding
    the packaged config a "real dispatch chain" test is supposed to exercise
    — fixed in ``tests/conftest.py``'s ``_ensure_delegation_routing_tiers_path``
    / ``_ensure_bifrost_contract_path``), not a defect in
    ``_select_model_for_task`` itself. This class still adds the coverage the
    ticket's AC4 asked for: the OMN-14396 id-match-then-use_for-scan loop
    (``for model in id_matches: if task_type in model.use_for: return model``)
    must find the ONE declaring candidate among 3+ id-colliding candidates
    regardless of its position in tier declaration order — a pure
    "return the first id match" bug would only surface once the declaring
    backend is NOT first, so position is varied deliberately below.
    """

    def _three_way_models(self, declaring_position: int) -> tuple[ModelTierModel, ...]:
        """Three backends sharing ``MODEL_QWEN3_35B_A3B`` as their id; only
        the one at ``declaring_position`` (0, 1, or 2) declares "research"."""
        use_for_by_position = [
            ("code_generation", "code_review"),
            ("code_generation", "code_review"),
            ("code_generation", "code_review"),
        ]
        use_for_by_position[declaring_position] = ("research", "reasoning")
        backend_refs = ("backend-alpha", "backend-beta", "backend-gamma")
        return tuple(
            ModelTierModel(
                id=MODEL_QWEN3_35B_A3B,
                backend_ref=backend_refs[i],
                max_context_tokens=65536,
                use_for=use_for_by_position[i],
                fast_path_threshold_tokens=None,
            )
            for i in range(3)
        )

    def _three_way_backends(self) -> dict[str, routing.BifrostBackendRef]:
        return {
            ref: routing.BifrostBackendRef(
                endpoint_url=f"https://{ref}.contract.test/v1/chat/completions",
                model_name=MODEL_QWEN3_35B_A3B,
                timeout_ms=30000,
                max_tokens=65536,
            )
            for ref in ("backend-alpha", "backend-beta", "backend-gamma")
        }

    @pytest.mark.parametrize(
        ("declaring_position", "expected_backend_ref"),
        [
            (0, "backend-alpha"),
            (1, "backend-beta"),
            (2, "backend-gamma"),
        ],
    )
    def test_declaring_backend_wins_regardless_of_position(
        self, declaring_position: int, expected_backend_ref: str
    ) -> None:
        selected = routing._select_model_for_task(
            self._three_way_models(declaring_position),
            "research",
            estimated_tokens=25,
            bifrost_backends=self._three_way_backends(),
            contract_model_ref=MODEL_QWEN3_35B_A3B,
            contract_model_ref_is_explicit_override=True,
        )

        assert selected is not None
        assert selected.backend_ref == expected_backend_ref, (
            "a 3-way id collision must resolve to whichever candidate "
            "actually declares the task type, regardless of its position "
            f"among the id matches; got {selected.backend_ref!r}, expected "
            f"{expected_backend_ref!r}"
        )
        assert "research" in selected.use_for

    def test_three_way_collision_falls_through_when_none_declare_it(
        self,
    ) -> None:
        """None of the 3 colliding backends declare "planning" — an implicit
        pin must return None rather than silently binding an off-capability
        backend just because it shares the pinned id."""
        selected = routing._select_model_for_task(
            self._three_way_models(declaring_position=0),
            "planning",
            estimated_tokens=25,
            bifrost_backends=self._three_way_backends(),
            contract_model_ref=MODEL_QWEN3_35B_A3B,
            contract_model_ref_is_explicit_override=False,
        )

        assert selected is None
