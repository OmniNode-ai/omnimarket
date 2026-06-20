# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""TDD coverage for HandlerExplorationPolicy (OMN-12844 / M4).

These tests are written from the contract BEFORE the handler exists. They prove
the six anti-lock-in invariants of the bandit exploration policy:

1. cold-start candidates are never starved (strictly positive selection prob),
2. a decayed past winner re-enters exploration,
3. bandit parameters come from the contract config, not module-level constants,
4. negative-control candidates are never the exploit winner,
5. the decision is deterministic given a seed,
6. the per-candidate probability distribution normalizes to 1.0.
"""

from __future__ import annotations

import importlib
import inspect
from datetime import UTC, datetime

import pytest

from omnimarket.nodes.node_context_exploration_policy_compute.handlers.handler_exploration_policy import (
    HandlerExplorationPolicy,
)
from omnimarket.nodes.node_context_exploration_policy_compute.models.model_exploration_policy_config import (
    EnumBanditFamily,
    ModelExplorationPolicyConfig,
)
from omnimarket.nodes.node_context_exploration_policy_compute.models.model_exploration_request import (
    ModelExplorationCandidate,
    ModelExplorationRequest,
)
from omnimarket.nodes.node_context_exploration_policy_compute.models.model_exploration_result import (
    ModelExplorationDecision,
)

_NOW = datetime(2026, 6, 8, 12, 0, 0, tzinfo=UTC)


def _candidate(
    capsule_hash: str,
    *,
    effectiveness_score: float = 0.5,
    decayed_confidence: float = 0.5,
    hit_count: int = 10,
    last_scored: datetime = _NOW,
    is_negative_control: bool = False,
) -> ModelExplorationCandidate:
    return ModelExplorationCandidate(
        capsule_hash=capsule_hash,
        effectiveness_score=effectiveness_score,
        decayed_confidence=decayed_confidence,
        hit_count=hit_count,
        last_scored=last_scored,
        is_negative_control=is_negative_control,
    )


def _config(
    *,
    family: EnumBanditFamily = EnumBanditFamily.EPSILON_GREEDY,
    epsilon: float = 0.2,
    ucb_c: float = 2.0,
    prior_alpha: float = 1.0,
    prior_beta: float = 1.0,
    min_trials_before_exploit: int = 3,
    decay_half_life_days: float = 14.0,
) -> ModelExplorationPolicyConfig:
    return ModelExplorationPolicyConfig(
        family=family,
        epsilon=epsilon,
        ucb_c=ucb_c,
        prior_alpha=prior_alpha,
        prior_beta=prior_beta,
        min_trials_before_exploit=min_trials_before_exploit,
        decay_half_life_days=decay_half_life_days,
    )


def _request(
    candidates: tuple[ModelExplorationCandidate, ...],
    config: ModelExplorationPolicyConfig,
    *,
    seed: int = 42,
    now: datetime = _NOW,
    experiment_cohort: str = "default",
) -> ModelExplorationRequest:
    return ModelExplorationRequest(
        candidates=candidates,
        config=config,
        seed=seed,
        now=now,
        experiment_cohort=experiment_cohort,
    )


def _prob_for(decision: ModelExplorationDecision, capsule_hash: str) -> float:
    for entry in decision.candidate_probabilities:
        if entry.capsule_hash == capsule_hash:
            return entry.probability
    raise AssertionError(f"no probability entry for {capsule_hash}")


@pytest.mark.unit
class TestColdStartNoStarvation:
    def test_greedy_winner_does_not_starve_coldstart(self) -> None:
        """A hit_count==0 candidate has strictly-positive selection probability
        even alongside two established high-score winners."""
        candidates = (
            _candidate(
                "hot-a",
                effectiveness_score=0.95,
                decayed_confidence=0.95,
                hit_count=200,
            ),
            _candidate(
                "hot-b",
                effectiveness_score=0.90,
                decayed_confidence=0.90,
                hit_count=150,
            ),
            _candidate(
                "cold", effectiveness_score=0.0, decayed_confidence=0.0, hit_count=0
            ),
        )
        decision = HandlerExplorationPolicy().handle(
            _request(candidates, _config(family=EnumBanditFamily.EPSILON_GREEDY))
        )
        assert _prob_for(decision, "cold") > 0.0

    @pytest.mark.parametrize(
        "family",
        [
            EnumBanditFamily.EPSILON_GREEDY,
            EnumBanditFamily.UCB1,
            EnumBanditFamily.THOMPSON,
        ],
    )
    def test_coldstart_positive_in_every_family(self, family: EnumBanditFamily) -> None:
        candidates = (
            _candidate(
                "hot", effectiveness_score=0.95, decayed_confidence=0.95, hit_count=200
            ),
            _candidate(
                "cold", effectiveness_score=0.0, decayed_confidence=0.0, hit_count=0
            ),
        )
        decision = HandlerExplorationPolicy().handle(
            _request(candidates, _config(family=family))
        )
        assert _prob_for(decision, "cold") > 0.0


@pytest.mark.unit
class TestDecayReentry:
    def test_decayed_winner_reenters_exploration(self) -> None:
        """A winner whose decayed confidence dropped below threshold gets a
        higher selection probability than when its confidence was high."""
        others = (
            _candidate(
                "rival", effectiveness_score=0.6, decayed_confidence=0.6, hit_count=120
            ),
            _candidate(
                "cold", effectiveness_score=0.0, decayed_confidence=0.0, hit_count=0
            ),
        )
        config = _config(family=EnumBanditFamily.EPSILON_GREEDY)

        high = _candidate(
            "winner", effectiveness_score=0.92, decayed_confidence=0.92, hit_count=300
        )
        high_decision = HandlerExplorationPolicy().handle(
            _request((high, *others), config)
        )
        prob_high = _prob_for(high_decision, "winner")

        decayed = _candidate(
            "winner", effectiveness_score=0.92, decayed_confidence=0.10, hit_count=300
        )
        decayed_decision = HandlerExplorationPolicy().handle(
            _request((decayed, *others), config)
        )
        prob_decayed = _prob_for(decayed_decision, "winner")

        # When confidence is high the winner exploits; when it decays it loses
        # exploit weight and re-enters the exploration pool. Selection prob of
        # the winner is LOWER once decayed -> rival/cold re-enter. The decay
        # re-entry property: cold/rival selection prob RISES post-decay.
        assert prob_decayed < prob_high
        assert _prob_for(decayed_decision, "rival") > _prob_for(high_decision, "rival")


@pytest.mark.unit
class TestContractConfigGovernsDistribution:
    def test_bandit_params_from_contract_not_constants(self) -> None:
        """Changing epsilon / family in the contract-resolved config changes the
        distribution, proving no module-level constant governs the policy."""
        candidates = (
            _candidate(
                "hot", effectiveness_score=0.95, decayed_confidence=0.95, hit_count=200
            ),
            _candidate(
                "warm", effectiveness_score=0.50, decayed_confidence=0.50, hit_count=80
            ),
        )
        low_eps = HandlerExplorationPolicy().handle(
            _request(
                candidates,
                _config(family=EnumBanditFamily.EPSILON_GREEDY, epsilon=0.01),
            )
        )
        high_eps = HandlerExplorationPolicy().handle(
            _request(
                candidates, _config(family=EnumBanditFamily.EPSILON_GREEDY, epsilon=0.5)
            )
        )
        assert _prob_for(low_eps, "warm") != _prob_for(high_eps, "warm")

        ucb = HandlerExplorationPolicy().handle(
            _request(candidates, _config(family=EnumBanditFamily.UCB1))
        )
        thompson = HandlerExplorationPolicy().handle(
            _request(candidates, _config(family=EnumBanditFamily.THOMPSON))
        )
        assert _prob_for(ucb, "hot") != _prob_for(thompson, "hot")

    def test_no_module_level_epsilon_constant(self) -> None:
        """Grep-guard: no module-level epsilon/EPSILON constant in handler src."""
        module = importlib.import_module(
            "omnimarket.nodes.node_context_exploration_policy_compute.handlers.handler_exploration_policy"
        )
        source = inspect.getsource(module)
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            lowered = stripped.lower()
            # No bare module-scope assignment of an epsilon/ucb_c/prior literal.
            assert not (
                lowered.startswith("epsilon")
                and "=" in lowered
                and "self." not in lowered
                and "config." not in lowered
            ), f"module-level epsilon constant found: {line!r}"
            assert "EPSILON =" not in line, (
                f"module-level EPSILON constant found: {line!r}"
            )


@pytest.mark.unit
class TestNegativeControlInvariant:
    def test_negative_control_never_exploit_winner(self) -> None:
        """A negative-control candidate is never selected as the exploit winner,
        even when it carries the highest effectiveness score."""
        candidates = (
            _candidate(
                "neg",
                effectiveness_score=0.99,
                decayed_confidence=0.99,
                hit_count=500,
                is_negative_control=True,
            ),
            _candidate(
                "real", effectiveness_score=0.70, decayed_confidence=0.70, hit_count=120
            ),
        )
        # Exploit-only config (epsilon 0) -> argmax exploit path.
        decision = HandlerExplorationPolicy().handle(
            _request(
                candidates, _config(family=EnumBanditFamily.EPSILON_GREEDY, epsilon=0.0)
            )
        )
        assert decision.selected_capsule_hash == "real"

    @pytest.mark.parametrize(
        "family",
        [
            EnumBanditFamily.EPSILON_GREEDY,
            EnumBanditFamily.UCB1,
            EnumBanditFamily.THOMPSON,
        ],
    )
    def test_negative_control_not_exploit_in_any_family(
        self, family: EnumBanditFamily
    ) -> None:
        candidates = (
            _candidate(
                "neg",
                effectiveness_score=0.99,
                decayed_confidence=0.99,
                hit_count=500,
                is_negative_control=True,
            ),
            _candidate(
                "real", effectiveness_score=0.70, decayed_confidence=0.70, hit_count=120
            ),
        )
        # Many seeds: the negative control must never be the chosen exploit
        # winner; with epsilon 0 it is never selected at all.
        for seed in range(25):
            decision = HandlerExplorationPolicy().handle(
                _request(
                    candidates,
                    _config(family=family, epsilon=0.0),
                    seed=seed,
                )
            )
            assert decision.selected_capsule_hash == "real"


@pytest.mark.unit
class TestDeterminism:
    def test_deterministic_given_seed(self) -> None:
        """Same candidates + config + seed -> identical ModelExplorationDecision."""
        candidates = (
            _candidate(
                "a", effectiveness_score=0.8, decayed_confidence=0.8, hit_count=50
            ),
            _candidate(
                "b", effectiveness_score=0.6, decayed_confidence=0.6, hit_count=30
            ),
            _candidate(
                "cold", effectiveness_score=0.0, decayed_confidence=0.0, hit_count=0
            ),
        )
        config = _config(family=EnumBanditFamily.THOMPSON)
        first = HandlerExplorationPolicy().handle(_request(candidates, config, seed=7))
        second = HandlerExplorationPolicy().handle(_request(candidates, config, seed=7))
        assert first == second

    def test_different_seeds_can_differ(self) -> None:
        candidates = (
            _candidate(
                "a", effectiveness_score=0.55, decayed_confidence=0.55, hit_count=40
            ),
            _candidate(
                "b", effectiveness_score=0.50, decayed_confidence=0.50, hit_count=40
            ),
            _candidate(
                "c", effectiveness_score=0.45, decayed_confidence=0.45, hit_count=40
            ),
        )
        config = _config(family=EnumBanditFamily.THOMPSON, epsilon=0.9)
        selections = {
            HandlerExplorationPolicy()
            .handle(_request(candidates, config, seed=s))
            .selected_capsule_hash
            for s in range(40)
        }
        assert len(selections) > 1


@pytest.mark.unit
class TestDistributionNormalizes:
    @pytest.mark.parametrize(
        "family",
        [
            EnumBanditFamily.EPSILON_GREEDY,
            EnumBanditFamily.UCB1,
            EnumBanditFamily.THOMPSON,
        ],
    )
    def test_distribution_normalizes(self, family: EnumBanditFamily) -> None:
        candidates = (
            _candidate(
                "a", effectiveness_score=0.9, decayed_confidence=0.9, hit_count=100
            ),
            _candidate(
                "b", effectiveness_score=0.5, decayed_confidence=0.5, hit_count=20
            ),
            _candidate(
                "cold", effectiveness_score=0.0, decayed_confidence=0.0, hit_count=0
            ),
            _candidate(
                "neg",
                effectiveness_score=0.8,
                decayed_confidence=0.8,
                hit_count=60,
                is_negative_control=True,
            ),
        )
        decision = HandlerExplorationPolicy().handle(
            _request(candidates, _config(family=family))
        )
        probs = [entry.probability for entry in decision.candidate_probabilities]
        assert all(p >= 0.0 for p in probs)
        assert pytest.approx(sum(probs), abs=1e-9) == 1.0


@pytest.mark.unit
class TestDecisionAuditSurface:
    def test_selection_reason_and_cohort_recorded(self) -> None:
        candidates = (
            _candidate(
                "a", effectiveness_score=0.8, decayed_confidence=0.8, hit_count=50
            ),
            _candidate(
                "cold", effectiveness_score=0.0, decayed_confidence=0.0, hit_count=0
            ),
        )
        decision = HandlerExplorationPolicy().handle(
            _request(
                candidates,
                _config(family=EnumBanditFamily.UCB1),
                experiment_cohort="cohort-xyz",
            )
        )
        assert decision.experiment_cohort == "cohort-xyz"
        assert decision.selection_reason
        assert decision.selected_capsule_hash in {"a", "cold"}
