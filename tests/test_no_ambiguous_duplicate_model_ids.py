# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Regression test for OMN-14435 (fan-out from OMN-14401).

Pins the current duplicate-model-id-collision state of routing_tiers.yaml at
zero ambiguous collisions, so a future PR that introduces a same-tier id
collision with overlapping (or missing) use_for is caught here even before
the check-duplicate-registry-ids pre-commit hook runs. Uses the existing
canonical parser (parse_delegation_config_yaml) already used by
test_routing_tiers_contract.py — no omnibase_core.validation import, so no
coupling to the OMN-14401 module landing in a not-yet-released omnibase_core
version at the time this PR was authored.

Mirrors ValidatorDuplicateConfigIds's per-tier-grouped, use_for-disjoint
disambiguation rule (src/omnibase_core/validation/validator_duplicate_config_ids.py
in omnibase_core) without importing it.
"""

from __future__ import annotations

from pathlib import Path

from omnibase_core.models.delegation.wire import ModelRoutingTier, ModelTierModel

from omnimarket.nodes.node_delegation_routing_reducer.models.model_delegation_config import (
    parse_delegation_config_yaml,
)

_CONFIG_PATH = (
    Path(__file__).parent.parent / "src/omnimarket/configs/routing_tiers.yaml"
)


def _same_tier_ambiguous_collisions(tier: ModelRoutingTier) -> dict[str, list[str]]:
    """Return {model_id: [overlapping use_for values]} for every id collision
    within this ONE tier that is NOT legitimately disambiguated.

    A collision is ambiguous iff two models share `id` and either one is
    missing `use_for` or their `use_for` sets overlap.
    """
    by_id: dict[str, list[tuple[str, ...]]] = {}
    for model in tier.models:
        by_id.setdefault(model.id, []).append(model.use_for)

    ambiguous: dict[str, list[str]] = {}
    for model_id, use_for_lists in by_id.items():
        if len(use_for_lists) < 2:
            continue
        for i in range(len(use_for_lists)):
            for j in range(i + 1, len(use_for_lists)):
                a, b = set(use_for_lists[i]), set(use_for_lists[j])
                overlap = a & b
                if not a or not b or overlap:
                    ambiguous.setdefault(model_id, []).extend(sorted(overlap))
    return ambiguous


def test_no_ambiguous_duplicate_model_ids_in_live_registry() -> None:
    """Every same-tier model id collision in the live file must be
    legitimately disambiguated by disjoint use_for sets.

    Measured 2026-07-12: 0 ambiguous collisions (the local tier's two
    Qwen3.6-35B-A3B entries are correctly disambiguated; cross-tier repeats
    of glm-5-turbo / openrouter-qwen3-coder-480b are a DIFFERENT tier each,
    out of scope for this per-tier check by design). This test pins that
    count so it can only ratchet down.
    """
    config = parse_delegation_config_yaml(_CONFIG_PATH.read_text(encoding="utf-8"))
    findings = {
        tier.name: collisions
        for tier in config.tiers
        if (collisions := _same_tier_ambiguous_collisions(tier))
    }
    assert findings == {}, (
        f"Ambiguous same-tier model id collision(s) in {_CONFIG_PATH}: {findings}. "
        "Two models in the same tier sharing an id must have disjoint use_for."
    )


def _model(model_id: str, use_for: tuple[str, ...]) -> ModelTierModel:
    return ModelTierModel(
        id=model_id,
        backend_ref="test-backend",
        max_context_tokens=8192,
        use_for=use_for,
        fast_path_threshold_tokens=None,
    )


def test_detector_actually_catches_a_same_tier_collision() -> None:
    """Adversarial check: the detector helper must fire on a real collision.

    Guards against a vacuous test — proves _same_tier_ambiguous_collisions is
    not a no-op that would pass on any input. Reproduces the exact OMN-14396
    shape: two backends sharing a model id with overlapping use_for.
    """
    tier = ModelRoutingTier(
        name="local",
        models=(
            _model("Qwen3.6-35B-A3B", ("code_generation", "research")),
            _model("Qwen3.6-35B-A3B", ("research", "reasoning")),
        ),
    )
    findings = _same_tier_ambiguous_collisions(tier)
    assert findings == {"Qwen3.6-35B-A3B": ["research"]}


def test_cross_tier_duplicate_does_not_false_positive() -> None:
    """Same id + same use_for in a DIFFERENT tier is legitimate (the real
    routing_tiers.yaml repeats glm-5-turbo across cheap_cloud and claude) —
    only checked within _same_tier_ambiguous_collisions, one tier at a time,
    so cross-tier repeats never reach this function at all."""
    tier_a = ModelRoutingTier(
        name="cheap_cloud", models=(_model("glm-5-turbo", ("code_generation",)),)
    )
    tier_b = ModelRoutingTier(
        name="claude", models=(_model("glm-5-turbo", ("code_generation",)),)
    )
    assert _same_tier_ambiguous_collisions(tier_a) == {}
    assert _same_tier_ambiguous_collisions(tier_b) == {}
