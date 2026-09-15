# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Within ``cheap_cloud``, the flat-rate GLM rung must precede the metered
Gemini rung for every task class both serve (OMN-13640).

RED-before, measured against the real config at ``omnimarket@1ae66ce1``
(origin/dev head at claim time): ``cloud-gemini-pro`` (model
``gemini-2.5-flash``) is declared FIRST in ``cheap_cloud.models``, and
``cloud-glm`` (model ``glm-5.3-flash``) is declared SECOND.
``_select_model_for_task`` uses declaration order as its tiebreaker when more
than one model in a tier declares the same ``task_type`` in ``use_for``, so
for every task class both backends serve — measured live to be
``code_generation``, ``code_review``, ``refactor``, ``reasoning`` and
``research`` — the resolver picks the METERED Gemini backend
(``cloud-gemini-pro``, a free-tier day quota of 20 requests, exhausted daily)
ahead of the FLAT-RATE ``cloud-glm`` backend. This inverts the standing
cheapest-first / GLM-preferred-over-Gemini policy
(``feedback_prefer_glm_over_gemini_outages``,
``feedback_delegation_chain_of_responders``).

The existing ``document`` / ``documentation`` / ``summarization`` prose
classes are NOT affected by this defect: ``cloud-gemini-pro`` does not declare
any of the three in its ``use_for``, so ``cloud-glm`` already resolves first
for them (see ``test_prose_free_tier_ladder_omn13640.py``, unchanged by this
fix). This test covers the CLASSES WHERE THE INVERSION IS LIVE — not the
formally-named "prose" set.

This test is evaluated over BEHAVIOUR (the real ``_select_model_for_task``),
not the raw YAML text, so a future edit that reorders declaration in a way
that still resolves Gemini first still fails here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
    handler_delegation_routing as routing,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_delegation_config import (
    ModelDelegationConfig,
    parse_delegation_config_yaml,
)

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_ROUTING_TIERS_PATH = _PROJECT_ROOT / "src/omnimarket/configs/routing_tiers.yaml"

_CHEAP_CLOUD_TIER = "cheap_cloud"
_GLM_BACKEND = "cloud-glm"
_GEMINI_PRO_BACKEND = "cloud-gemini-pro"

# Task classes measured live (2026-09-15, real config) to be declared in
# BOTH cloud-gemini-pro's and cloud-glm's cheap_cloud use_for lists — i.e.
# every class where the ordering tiebreaker actually matters. Named literally
# so a class that stops being dual-eligible is caught here rather than
# silently dropping out of coverage.
_DUAL_ELIGIBLE_TASK_CLASSES: tuple[str, ...] = (
    "code_generation",
    "code_review",
    "refactor",
    "reasoning",
    "research",
)


def _routing_config() -> ModelDelegationConfig:
    return parse_delegation_config_yaml(_ROUTING_TIERS_PATH.read_text())


def _synthetic_available_backends(
    config: ModelDelegationConfig,
) -> dict[str, routing.BifrostBackendRef]:
    """Deterministic endpoint/secret availability for a structural test.

    Mirrors ``test_prose_free_tier_ladder_omn13640._synthetic_available_backends``
    so this proves ``use_for`` + declaration-order behaviour, never live
    provider/network/credential state.
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
                    max_tokens=4096,
                ),
            )
    return backends


@pytest.mark.unit
def test_both_backends_are_still_declared_in_cheap_cloud() -> None:
    """Positive control: both backends must actually be present and eligible.

    Vacuous (falsely green) if either backend stops being declared in
    ``cheap_cloud`` for these classes — asserted directly rather than assumed.
    """
    config = _routing_config()
    by_name = {tier.name: tier for tier in config.tiers}
    cheap_cloud = by_name[_CHEAP_CLOUD_TIER]
    backend_refs = {model.backend_ref for model in cheap_cloud.models}
    assert _GLM_BACKEND in backend_refs, (
        f"{_GLM_BACKEND} is no longer declared in {_CHEAP_CLOUD_TIER}"
    )
    assert _GEMINI_PRO_BACKEND in backend_refs, (
        f"{_GEMINI_PRO_BACKEND} is no longer declared in {_CHEAP_CLOUD_TIER}"
    )

    for task_type in _DUAL_ELIGIBLE_TASK_CLASSES:
        serving = [
            model.backend_ref
            for model in cheap_cloud.models
            if task_type in model.use_for
            and model.backend_ref in (_GLM_BACKEND, _GEMINI_PRO_BACKEND)
        ]
        assert set(serving) == {_GLM_BACKEND, _GEMINI_PRO_BACKEND}, (
            f"{task_type} is no longer served by both {_GLM_BACKEND} and "
            f"{_GEMINI_PRO_BACKEND} in {_CHEAP_CLOUD_TIER} — the ordering "
            "question this test asks is vacuous for it"
        )


@pytest.mark.unit
def test_glm_resolves_before_gemini_for_every_dual_eligible_cheap_cloud_class() -> None:
    """The flat-rate GLM backend must win the declaration-order tiebreak.

    Evaluated through the real ``_select_model_for_task`` with
    ``contract_model_ref=None`` (no pin), so this proves the actual resolver
    behaviour a task-type-only call gets, not the raw YAML list order.
    """
    config = _routing_config()
    backends = _synthetic_available_backends(config)
    by_name = {tier.name: tier for tier in config.tiers}
    cheap_cloud = by_name[_CHEAP_CLOUD_TIER]

    inverted: dict[str, str] = {}
    for task_type in _DUAL_ELIGIBLE_TASK_CLASSES:
        selected = routing._select_model_for_task(
            cheap_cloud.models,
            task_type,
            0,
            backends,
            contract_model_ref=None,
        )
        assert selected is not None, (
            f"{task_type} resolved no model at all on {_CHEAP_CLOUD_TIER}"
        )
        if selected.backend_ref != _GLM_BACKEND:
            inverted[task_type] = (
                f"resolved {selected.backend_ref}, expected {_GLM_BACKEND} "
                "(flat-rate) ahead of cloud-gemini-pro (metered, 20/day free "
                "quota) per the standing GLM-over-Gemini cheapest-first policy"
            )

    assert inverted == {}, (
        f"cheap_cloud classes where the metered Gemini backend still wins the "
        f"declaration-order tiebreak over flat-rate GLM: {inverted}"
    )
