# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Exploration policy handler — pure, deterministic bandit selection.

Replaces the greedy argmax in ``node_context_roi_compute`` (``_select_preferred_arm``)
with a probabilistic explore/exploit policy over scored, decay-adjusted capsule
candidates. This prevents two failure modes of greedy selection:

* cold-start starvation -- a fresh capsule with no history is never selected and
  so never earns history;
* winner lock-in -- a past winner is selected forever even after its surrounding
  code drifts and its M2 decayed confidence collapses.

Purity / contract-native invariants:
* No I/O, no bus, no inference, no ``datetime.now()``, no ambient ``random()``.
* Every bandit parameter is read from the contract-resolved
  ``ModelExplorationPolicyConfig`` on the request -- there is NO module-level
  epsilon / coefficient constant.
* Randomness is derived from the typed ``seed`` on the request, so the same
  (candidates, config, seed) always yields the same ``ModelExplorationDecision``.
* The policy ranks on M2's already-decayed ``decayed_confidence``; it never
  recomputes decay.
"""

from __future__ import annotations

import math
import random

from omnimarket.nodes.node_context_exploration_policy_compute.models.model_exploration_policy_config import (
    EnumBanditFamily,
    ModelExplorationPolicyConfig,
)
from omnimarket.nodes.node_context_exploration_policy_compute.models.model_exploration_request import (
    ModelExplorationCandidate,
    ModelExplorationRequest,
)
from omnimarket.nodes.node_context_exploration_policy_compute.models.model_exploration_result import (
    EnumSelectionReason,
    ModelCandidateProbability,
    ModelExplorationDecision,
)

# Numerical floor guaranteeing a cold-start candidate always retains strictly
# positive mass after blending, independent of how dominant an established
# winner is. This is a numerical-stability epsilon, NOT a bandit policy
# parameter -- every policy parameter (epsilon/ucb_c/priors/...) lives on the
# contract-resolved config, never here.
_COLD_START_FLOOR: float = 1e-3


def _is_cold_start(
    candidate: ModelExplorationCandidate, config: ModelExplorationPolicyConfig
) -> bool:
    return candidate.hit_count < config.min_trials_before_exploit


def _exploit_weight(
    candidate: ModelExplorationCandidate,
    config: ModelExplorationPolicyConfig,
    total_trials: int,
    rng: random.Random,
) -> float:
    """Per-candidate exploit weight for the configured bandit family.

    Ranks on the already-decayed confidence (M2 owns the decay math). A larger
    weight means stronger exploitation pull. Cold-start candidates are NOT given
    exploit dominance here -- their selection floor is applied separately so a
    fresh candidate can never be starved by an established winner.
    """
    if config.family == EnumBanditFamily.EPSILON_GREEDY:
        # Pure exploit signal: the decayed confidence. The epsilon-driven
        # uniform spread is blended in by the caller.
        return candidate.decayed_confidence

    if config.family == EnumBanditFamily.UCB1:
        # Upper confidence bound: decayed confidence plus an uncertainty bonus
        # that shrinks as the candidate accrues trials. n == 0 yields an
        # unbounded bonus, so an untried candidate dominates exploitation.
        if candidate.hit_count == 0:
            return math.inf
        bonus = config.ucb_c * math.sqrt(
            math.log(total_trials + 1) / candidate.hit_count
        )
        return candidate.decayed_confidence + bonus

    # THOMPSON: sample a Beta posterior whose mass concentrates around the
    # decayed confidence, with the trial count controlling concentration. A
    # low-trial candidate has a wide posterior and can sample high (exploration);
    # a high-trial candidate concentrates near its decayed confidence.
    concentration = float(candidate.hit_count)
    alpha = config.prior_alpha + candidate.decayed_confidence * concentration
    beta = config.prior_beta + (1.0 - candidate.decayed_confidence) * concentration
    return rng.betavariate(alpha, beta)


def _eligible_distribution(
    candidates: tuple[ModelExplorationCandidate, ...],
    config: ModelExplorationPolicyConfig,
    rng: random.Random,
) -> dict[str, float]:
    """Build the un-blended exploit distribution over eligible candidates.

    Negative controls are excluded from the eligible exploit pool: they are
    never the exploit winner. The returned mapping covers eligible candidates
    only; callers add the exploration spread and cold-start floor.
    """
    eligible = [c for c in candidates if not c.is_negative_control]
    total_trials = sum(c.hit_count for c in eligible)

    weights = {
        c.capsule_hash: _exploit_weight(c, config, total_trials, rng) for c in eligible
    }

    # UCB1 cold-start: any infinite weight means at least one untried candidate
    # must take the exploit mass; spread exploit mass uniformly across the
    # infinite-weight set so the distribution stays finite and normalizable.
    infinite = [h for h, w in weights.items() if math.isinf(w)]
    if infinite:
        share = 1.0 / len(infinite)
        return {h: (share if h in infinite else 0.0) for h in weights}

    total_weight = sum(weights.values())
    if total_weight <= 0.0:
        # No exploit signal at all (all decayed confidences zero) -> uniform
        # over eligible candidates.
        share = 1.0 / len(eligible) if eligible else 0.0
        return dict.fromkeys(weights, share)

    return {h: w / total_weight for h, w in weights.items()}


class HandlerExplorationPolicy:
    """COMPUTE -- pure probabilistic explore/exploit selection over capsules."""

    def handle(self, request: ModelExplorationRequest) -> ModelExplorationDecision:
        config = request.config
        candidates = request.candidates
        rng = random.Random(request.seed)

        eligible = [c for c in candidates if not c.is_negative_control]
        if not eligible:
            raise ValueError(
                "No eligible (non-negative-control) candidates to select among."
            )

        exploit = _eligible_distribution(candidates, config, rng)

        # Blend exploit with a uniform-exploration component over eligible
        # candidates. epsilon (from the contract) controls how much mass is
        # spread uniformly vs concentrated on the exploit distribution.
        uniform_share = 1.0 / len(eligible)
        blended: dict[str, float] = {}
        for candidate in eligible:
            h = candidate.capsule_hash
            blended[h] = (1.0 - config.epsilon) * exploit[h] + (
                config.epsilon * uniform_share
            )

        # Cold-start floor: any candidate below min_trials_before_exploit is
        # guaranteed strictly positive mass so it can never be starved.
        for candidate in eligible:
            if _is_cold_start(candidate, config):
                h = candidate.capsule_hash
                blended[h] = max(blended[h], _COLD_START_FLOOR)

        # Negative controls are explicitly assigned zero mass (never exploited).
        for candidate in candidates:
            if candidate.is_negative_control:
                blended[candidate.capsule_hash] = 0.0

        total = sum(blended.values())
        normalized = {h: p / total for h, p in blended.items()}

        # Deterministic sampling from the normalized distribution using the
        # seeded RNG over a stable candidate ordering.
        selected_hash, reason_class = self._sample(candidates, normalized, config, rng)
        selected = next(c for c in candidates if c.capsule_hash == selected_hash)

        reason = self._reason_text(selected, config, reason_class, normalized)
        probabilities = tuple(
            ModelCandidateProbability(
                capsule_hash=c.capsule_hash,
                probability=normalized[c.capsule_hash],
                effectiveness_score=c.effectiveness_score,
                decayed_confidence=c.decayed_confidence,
                hit_count=c.hit_count,
                is_cold_start=_is_cold_start(c, config),
                is_negative_control=c.is_negative_control,
            )
            for c in candidates
        )

        return ModelExplorationDecision(
            selected_capsule_hash=selected_hash,
            selection_reason=reason,
            selection_reason_class=reason_class,
            family=config.family,
            experiment_cohort=request.experiment_cohort,
            candidate_probabilities=probabilities,
        )

    @staticmethod
    def _sample(
        candidates: tuple[ModelExplorationCandidate, ...],
        normalized: dict[str, float],
        config: ModelExplorationPolicyConfig,
        rng: random.Random,
    ) -> tuple[str, EnumSelectionReason]:
        """Draw one candidate from the normalized distribution (seeded)."""
        ordered = [c.capsule_hash for c in candidates]
        draw = rng.random()
        cumulative = 0.0
        chosen = ordered[-1]
        for h in ordered:
            cumulative += normalized[h]
            if draw <= cumulative:
                chosen = h
                break

        chosen_candidate = next(c for c in candidates if c.capsule_hash == chosen)
        if _is_cold_start(chosen_candidate, config):
            return chosen, EnumSelectionReason.COLD_START

        # Exploit vs explore: the exploit winner is the eligible candidate with
        # the highest decayed confidence; selecting anything else is explore.
        eligible = [c for c in candidates if not c.is_negative_control]
        exploit_winner = max(eligible, key=lambda c: c.decayed_confidence)
        if chosen == exploit_winner.capsule_hash:
            return chosen, EnumSelectionReason.EXPLOIT
        return chosen, EnumSelectionReason.EXPLORE

    @staticmethod
    def _reason_text(
        selected: ModelExplorationCandidate,
        config: ModelExplorationPolicyConfig,
        reason_class: EnumSelectionReason,
        normalized: dict[str, float],
    ) -> str:
        prob = normalized[selected.capsule_hash]
        return (
            f"{reason_class.value}: family={config.family.value} "
            f"selected '{selected.capsule_hash}' "
            f"(p={prob:.4f}, decayed_confidence={selected.decayed_confidence:.4f}, "
            f"hit_count={selected.hit_count}, epsilon={config.epsilon:.4f})."
        )


__all__ = ["HandlerExplorationPolicy"]
