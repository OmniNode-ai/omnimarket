# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Compaction verdict compute: the replay decision and the live block interval.

HandlerCompactionVerdict turns a corpus of boundaries, each scored under the
``mod`` and ``stock_rerun`` arms, into one verdict report:

1. matched removal: a boundary unscored in either arm leaves both arms, and
   every removal is counted per arm and reason;
2. the mod fallback share; above ``fallback_limit`` the verdict is ``HOLD``
   naming ``MOD_FALLBACK`` (first in precedence);
3. the session-clustered paired bootstrap of every primary and safeguard,
   Holm over the primaries, the noninferiority safeguards with the
   session-level rule-of-three bound when both arms have zero events, the
   feasibility rule (a safeguard whose margin is at most 3 / sessions makes
   the run ``EXPLORATORY``, which cannot issue ``HELPS`` or ``NO_DIFFERENCE``);
4. per-arm Wilson intervals as a display only;
5. the joint power simulation at this corpus shape, and the refusal of a
   confirmatory run below the minimum session count or the minimum power.

Every number comes from the thresholds data file named in the request; the
handler packages none. HandlerLiveBlockInterval is the live stage's
block-paired bootstrap: it resamples whole randomized blocks of one session
per arm.
"""

from __future__ import annotations

import random
from collections import Counter

from omnimarket.handlers.stats_wilson import WILSON_CONFIDENCE, wilson_interval
from omnimarket.nodes.node_compaction_verdict_compute.handlers.joint_power import (
    simulate_joint_power,
)
from omnimarket.nodes.node_compaction_verdict_compute.handlers.thresholds_loader import (
    load_thresholds,
)
from omnimarket.nodes.node_compaction_verdict_compute.handlers.verdict_stats import (
    evaluate,
    metric_names,
    session_vectors,
)
from omnimarket.nodes.node_compaction_verdict_compute.models import (
    CompactionVerdictRefusedError,
    EnumCompactionVerdict,
    EnumRunLabel,
    ModelArmDisplay,
    ModelCompactionVerdictReport,
    ModelCompactionVerdictRequest,
    ModelLiveInterval,
    ModelLiveIntervalRequest,
    ModelPairedBoundary,
    ModelRemovalCount,
)

__all__ = ["HandlerCompactionVerdict", "HandlerLiveBlockInterval"]

_VERDICT_SEED = 20261102
_MOD_FALLBACK = "MOD_FALLBACK"
_MATCHED = "matched_removal"


def _removals(
    boundaries: list[ModelPairedBoundary],
) -> tuple[list[ModelPairedBoundary], tuple[ModelRemovalCount, ...]]:
    kept: list[ModelPairedBoundary] = []
    counts: Counter[tuple[str, str]] = Counter()
    for cell in boundaries:
        if cell.mod.scored and cell.stock.scored:
            kept.append(cell)
            continue
        for arm, own in (("mod", cell.mod), ("stock", cell.stock)):
            reason = (own.unscored_reason or "unscored") if not own.scored else _MATCHED
            counts[(arm, reason)] += 1
    rows = tuple(
        ModelRemovalCount(
            arm="mod" if arm == "mod" else "stock", reason=reason, count=n
        )
        for (arm, reason), n in sorted(counts.items())
    )
    return kept, rows


class HandlerCompactionVerdict:
    """Replay verdict of the compaction mod against the stock rerun."""

    def handle(
        self, request: ModelCompactionVerdictRequest
    ) -> ModelCompactionVerdictReport:
        thresholds = load_thresholds(request.thresholds_path)
        ordered = sorted(
            request.boundaries, key=lambda c: (c.session_id, c.boundary_id)
        )
        keys = [(c.session_id, c.boundary_id) for c in ordered]
        if len(set(keys)) != len(keys):
            raise CompactionVerdictRefusedError("a boundary appears more than once")
        fallback_share = sum(c.mod.fell_back for c in ordered) / len(ordered)
        kept, removals = _removals(ordered)
        if not kept:
            raise CompactionVerdictRefusedError("no boundary is scored in both arms")

        metrics = metric_names(thresholds)
        vectors = session_vectors(kept, metrics)
        sessions = len(vectors)
        if request.confirmatory and sessions < thresholds.minimum_sessions:
            raise CompactionVerdictRefusedError(
                f"confirmatory run refused: {sessions} sessions is below "
                f"minimum_sessions={thresholds.minimum_sessions}"
            )
        power = simulate_joint_power(kept, thresholds)
        below = (
            sessions < thresholds.minimum_sessions
            or power.simulated_power < thresholds.minimum_power
        )
        if request.confirmatory and below:
            raise CompactionVerdictRefusedError(
                f"confirmatory run refused: simulated power {power.simulated_power:.3f} "
                f"is below minimum_power={thresholds.minimum_power}"
            )

        outcome = evaluate(
            vectors,
            thresholds,
            metrics,
            resamples=thresholds.bootstrap_resamples,
            seed=_VERDICT_SEED,
        )
        hold = fallback_share > thresholds.fallback_limit
        verdict = EnumCompactionVerdict.HOLD if hold else outcome.verdict
        if outcome.exploratory:
            label = EnumRunLabel.EXPLORATORY
        elif below:
            label = EnumRunLabel.NOT_POWERED
        elif request.confirmatory:
            label = EnumRunLabel.CONFIRMATORY
        else:
            label = EnumRunLabel.PILOT

        display: list[ModelArmDisplay] = []
        for metric in metrics:
            for arm in ("mod", "stock"):
                successes = trials = 0
                for cell in kept:
                    count = (cell.mod if arm == "mod" else cell.stock).metrics.get(
                        metric
                    )
                    if count is not None:
                        successes += count.successes
                        trials += count.trials
                low, high = wilson_interval(successes, trials)
                display.append(
                    ModelArmDisplay(
                        arm="mod" if arm == "mod" else "stock",
                        metric=metric,
                        successes=successes,
                        trials=trials,
                        wilson_low=low,
                        wilson_high=high,
                        wilson_confidence=WILSON_CONFIDENCE,
                    )
                )

        return ModelCompactionVerdictReport(
            run_id=request.run_id,
            verdict=verdict,
            run_label=label,
            hold_reason=_MOD_FALLBACK if hold else None,
            sessions=sessions,
            boundaries_used=len(kept),
            fallback_share=fallback_share,
            removals=removals,
            primaries=outcome.primaries,
            safeguards=outcome.safeguards,
            arm_display=tuple(display),
            power=power,
        )


class HandlerLiveBlockInterval:
    """Block-paired percentile bootstrap of a live mod-minus-stock difference."""

    def handle(self, request: ModelLiveIntervalRequest) -> ModelLiveInterval:
        differences: list[float] = []
        for block in request.blocks:
            mod, stock = block.mod.metric, block.stock.metric
            if mod.trials == 0 or stock.trials == 0:
                raise ValueError(f"block {block.block_id} has an arm with no items")
            differences.append(
                mod.successes / mod.trials - stock.successes / stock.trials
            )
        count = len(differences)
        rng = random.Random(request.seed)
        means = sorted(
            sum(rng.choices(differences, k=count)) / count
            for _ in range(request.resamples)
        )
        tail = (1.0 - request.confidence) / 2.0
        low = means[max(0, int(tail * request.resamples))]
        high = means[min(request.resamples - 1, int((1.0 - tail) * request.resamples))]
        return ModelLiveInterval(
            metric=request.metric,
            blocks=count,
            estimate=sum(differences) / count,
            low=low,
            high=high,
            confidence=request.confidence,
        )
