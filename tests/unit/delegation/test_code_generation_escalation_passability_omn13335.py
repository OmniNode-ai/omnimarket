# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 OmniNode Team
"""Contract-invariant guard for the OMN-13335 escalation discriminator.

OMN-13335 (the SEA customer-beta discriminator) requires that a fresh
``code_generation`` delegation can reach a terminal with ALL THREE of:

  * ``escalation_count >= 1`` (a real up-tier escalation happened),
  * a non-local CLOUD ``model_name`` (the escalation reached a capable cloud
    ceiling, NOT a local Qwen / ds-v4-flash), and
  * ``quality_gate_passed = true`` (the cloud answer cleared the required bar).

The routing layer already PRODUCES this terminal on the judge-fix image
(OMN-13470, dev-lane proof row ``a66b6c6b``: code_generation / escalation_count=2
/ gemini-2.5-flash / quality_gate_passed=t). This module does NOT re-prove the
live run — that is OMN-13335's stability-lane DoD. It pins the three CONTRACT
PROPERTIES that make that terminal structurally reachable, so a future config
edit cannot silently re-break the discriminator (the exact regression that made
it unreachable before OMN-13351 / OMN-13161 / OMN-13470):

  1. code_generation's escalation_policy.tier_order ends at a CLOUD ceiling tier
     whose model resolves to a non-local, key-resolvable cloud backend — NOT a
     local tier and NOT the dead Anthropic ``cloud-sonnet`` (provider-agnostic:
     Gemini or GLM, never ``claude`` as a provider).
  2. that ceiling backend carries an adequate output budget (max_tokens) so a
     correct code answer is not truncated below the bar (OMN-13161 raised the
     up-tier budgets to 65536; a truncation regression below this floor would
     make the cloud answer score sub-bar and FAIL the discriminator).
  3. the OMN-13470 judge combine lifts a mechanically-incomplete-but-correct
     cloud answer over code_generation's required_bar (0.85): the deterministic
     hard floor passes (1.0) and a passing judge score clears the bar. A
     deterministic-only gate scores such an answer ~0.733 < 0.85 and FAILS —
     exactly the pre-judge-fix "cloud-escalation-but-QG-FAILED" mode.

These are STATIC contract/resolution assertions (no broker, no inference), so
they run in CI and pre-commit and fail loud the moment the discriminator becomes
unreachable by config drift.

Related:
    - OMN-13335: escalation up-tier proof-run discriminator (this guard)
    - OMN-13351: ceiling repointed off dead Anthropic cloud-sonnet → cloud-gemini-pro
    - OMN-13161: up-tier max_tokens raised to the routing contract (65536)
    - OMN-13470: judge-adequacy combine into the quality gate
    - OMN-13380: cheap_cloud GLM-5.2 primary for code_generation
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_delegation_orchestrator.quality_bar_authority import (
    resolve_required_bar_authority,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    _COMBINED_DETERMINISTIC_WEIGHT,
    _COMBINED_JUDGE_WEIGHT,
    _combined_quality_score,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    _get_config,
    _get_contract_model_ref,
    _get_task_class_contract,
    _load_bifrost_endpoints,
    _task_class_entry,
    _tier_order_from_contract,
)

# The code_generation task class under test.
_TASK_TYPE = "code_generation"

# Local backend_ids that must NOT be the escalation ceiling (the discriminator
# requires the terminal model to be a non-local cloud model). Sourced from the
# ``local`` tier of routing_tiers.yaml.
_LOCAL_BACKEND_IDS: frozenset[str] = frozenset(
    {
        "local-coder",
        "local-reasoner",
        "local-heavy-reasoning",
        "local-ds-v4-flash",
        "local-embedding",
    }
)

# The minimum output budget the ceiling backend must carry so a complete code
# answer is not truncated below the bar. OMN-13161 raised the cloud + local
# code budgets to 65536; this floor is intentionally generous (a single full
# module/function easily fits) and any drift below it is a real regression.
_MIN_CEILING_MAX_TOKENS = 16384

# Provider tokens that, if they appeared as the ceiling backend's resolved
# model_name, would mean the discriminator routes to a model this org cannot
# call (no Anthropic/OpenAI key exists org-wide — see feedback memory). The
# ceiling must be Gemini or GLM, never an Anthropic/OpenAI model.
_FORBIDDEN_PROVIDER_TOKENS: tuple[str, ...] = (
    "claude",
    "anthropic",
    "gpt-4",
    "gpt-3",
    "o1-",
    "o3-",
)


@pytest.mark.unit
def test_code_generation_tier_order_ceiling_is_cloud_not_local() -> None:
    """The code_generation tier_order must END at a cloud tier, not a local tier.

    The discriminator requires a CLOUD terminal after escalation. If the
    contract-declared ceiling tier were ``local`` the escalation could only ever
    terminate on a local Qwen / ds-v4-flash, which fails the discriminator's
    "non-local model_name" clause by construction.
    """
    config = _get_config()
    contract = _get_task_class_contract()
    entry = _task_class_entry(contract, _TASK_TYPE)
    assert entry is not None, "code_generation task class must be declared"

    ordered = _tier_order_from_contract(config, entry)
    assert ordered, "code_generation must declare a non-empty tier_order"

    ceiling = ordered[-1]
    assert ceiling.name != "local", (
        "code_generation escalation ceiling must be a cloud tier, not 'local' — "
        f"resolved ceiling tier '{ceiling.name}'. A local ceiling makes the "
        "OMN-13335 cloud-escalation discriminator unreachable."
    )
    # At least one non-local tier must appear AFTER a local tier somewhere in the
    # ladder, so a local-first attempt can escalate UP into cloud (the discriminator
    # shape). The ladder may start cloud-first (OMN-13380 GLM primary), but a cloud
    # tier strictly after the first position is what lets escalation_count>=1 reach
    # cloud when an earlier attempt is rejected.
    cloud_tiers = [t for t in ordered if t.name != "local"]
    assert cloud_tiers, "code_generation tier_order must contain a cloud tier"


@pytest.mark.unit
def test_code_generation_ceiling_resolves_to_keyed_cloud_backend() -> None:
    """The ceiling tier's code_generation model must resolve to a cloud backend.

    Pins that the contract-resolved ceiling model maps to a backend that (a) is
    NOT a local backend_id, (b) carries a resolvable secret_ref (key), and (c)
    declares a model_name that is provider-correct (Gemini/GLM, never an
    Anthropic/OpenAI model — this org holds no such key).
    """
    config = _get_config()
    contract = _get_task_class_contract()
    entry = _task_class_entry(contract, _TASK_TYPE)
    assert entry is not None

    ordered = _tier_order_from_contract(config, entry)
    ceiling = ordered[-1]

    # The model serving code_generation in the ceiling tier.
    ceiling_models = [m for m in ceiling.models if _TASK_TYPE in m.use_for]
    assert ceiling_models, (
        f"ceiling tier '{ceiling.name}' declares no model serving "
        f"{_TASK_TYPE!r} — escalation cannot route code_generation to it."
    )
    ceiling_model = ceiling_models[0]

    assert ceiling_model.backend_ref not in _LOCAL_BACKEND_IDS, (
        f"code_generation ceiling model '{ceiling_model.id}' resolves to a LOCAL "
        f"backend '{ceiling_model.backend_ref}'; the discriminator requires a "
        "cloud terminal."
    )

    backends = _load_bifrost_endpoints()
    backend = backends.get(ceiling_model.backend_ref)
    assert backend is not None, (
        f"ceiling backend '{ceiling_model.backend_ref}' did not resolve from the "
        "bifrost contract (missing endpoint_url/model_name?). Without a resolvable "
        "ceiling backend the cloud escalation terminal cannot be produced."
    )
    assert backend.api_key_ref, (
        f"ceiling backend '{ceiling_model.backend_ref}' carries no resolvable "
        "secret_ref — a keyless ceiling terminates 'no_routable_backend_for_task' "
        "(the OMN-13351 dead-cloud-sonnet failure mode)."
    )

    model_name_lc = (backend.model_name or "").lower()
    for token in _FORBIDDEN_PROVIDER_TOKENS:
        assert token not in model_name_lc, (
            f"code_generation ceiling backend model_name '{backend.model_name}' "
            f"contains forbidden provider token '{token}'. No Anthropic/OpenAI key "
            "exists org-wide; the ceiling must be Gemini or GLM."
        )


@pytest.mark.unit
def test_code_generation_ceiling_max_tokens_adequate() -> None:
    """The ceiling backend must carry an adequate output budget (no truncation).

    A correct code answer truncated below the bar scores sub-bar and FAILS the
    discriminator. OMN-13161 raised the up-tier budget to 65536; this guard fails
    loud on any drift below the floor.
    """
    config = _get_config()
    contract = _get_task_class_contract()
    entry = _task_class_entry(contract, _TASK_TYPE)
    assert entry is not None

    ordered = _tier_order_from_contract(config, entry)
    ceiling = ordered[-1]
    ceiling_model = next(m for m in ceiling.models if _TASK_TYPE in m.use_for)

    backends = _load_bifrost_endpoints()
    backend = backends[ceiling_model.backend_ref]
    assert backend.max_tokens >= _MIN_CEILING_MAX_TOKENS, (
        f"code_generation ceiling backend '{ceiling_model.backend_ref}' max_tokens="
        f"{backend.max_tokens} is below the {_MIN_CEILING_MAX_TOKENS} floor — a "
        "truncated cloud answer scores sub-bar and fails the discriminator."
    )


@pytest.mark.unit
def test_judge_combine_lifts_codegen_answer_over_required_bar() -> None:
    """A passing judge score must lift a clean code answer over required_bar.

    For code_generation the deterministic floor (3 checks) fully passes on a real
    answer → deterministic_fraction 1.0. The combined score is then
    ``0.6*1.0 + 0.4*judge``. This must clear required_bar (0.85) for an
    achievable judge score; otherwise the discriminator is mathematically
    unreachable even with a correct cloud answer.
    """
    authority = resolve_required_bar_authority(task_type=_TASK_TYPE)
    required_bar = authority.required_bar
    assert required_bar == pytest.approx(0.85), (
        f"code_generation required_bar drifted to {required_bar}; the combine math "
        "guard below is calibrated against 0.85."
    )

    # Weights are the deployed combine weights; pin them so a weight edit that
    # would change passability is caught here.
    assert pytest.approx(0.6) == _COMBINED_DETERMINISTIC_WEIGHT
    assert pytest.approx(0.4) == _COMBINED_JUDGE_WEIGHT

    # Deterministic floor fully passed (real code answer) → fraction 1.0.
    # The judge score required to exactly reach the bar:
    #   0.6*1.0 + 0.4*j = 0.85  ->  j = (0.85 - 0.6) / 0.4 = 0.625
    breakeven_judge = (
        required_bar - _COMBINED_DETERMINISTIC_WEIGHT
    ) / _COMBINED_JUDGE_WEIGHT
    assert breakeven_judge <= 0.85, (
        "the judge score needed to clear required_bar with a passing deterministic "
        f"floor is {breakeven_judge:.3f}; if this exceeds a normal passing judge "
        "score (~0.85+) the discriminator is unreachable and the bar/weights need "
        "a deliberate, owner-approved tune."
    )

    # A solidly-passing judge score clears the bar.
    combined_pass = _combined_quality_score(
        deterministic_score=1.0, judge_adequacy_score=0.85
    )
    assert combined_pass >= required_bar, (
        f"combined score {combined_pass} with a 0.85 judge does not clear "
        f"required_bar {required_bar} — discriminator unreachable."
    )

    # A refusal/empty cloud answer (deterministic floor fails) is NOT lifted: the
    # combine is only reached when the deterministic floor passes (handler_quality_gate
    # hard-blocks on det_failures before the combine), so a high judge score on a
    # broken answer cannot fake a pass. Pinned here as the lower bound of the band:
    combined_low_judge = _combined_quality_score(
        deterministic_score=1.0, judge_adequacy_score=0.0
    )
    assert combined_low_judge < required_bar, (
        "a clean answer with a zero judge score must stay below the bar so the "
        "judge band remains load-bearing (not a rubber stamp)."
    )


@pytest.mark.unit
def test_code_generation_contract_model_ref_is_resolvable() -> None:
    """The code_generation default model override must resolve in the contract.

    A dangling task_model_override (e.g. an id not present in any tier) would
    strand routing. Pins that the declared code_generation model ref is real.
    """
    contract = _get_task_class_contract()
    model_ref = _get_contract_model_ref(_TASK_TYPE, contract=contract)
    assert model_ref, "code_generation must declare a resolvable task_model_override"

    config = _get_config()
    all_model_ids = {m.id for tier in config.tiers for m in tier.models}
    assert model_ref in all_model_ids, (
        f"code_generation task_model_override '{model_ref}' is not present in any "
        f"routing tier — dangling ref, routing would strand."
    )
