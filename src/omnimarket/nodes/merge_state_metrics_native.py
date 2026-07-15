# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Merge-flow telemetry metrics (OMN-14648 / WS6) — pure measurement layer.

Deterministic, no-I/O fold from a list of ``ModelMergeStateTransitionEvent``
into ``ModelMergeFlowMetrics``. This is the MEASUREMENT the operator asked for
*before* any auto-merge automation ("measure before automating"): it quantifies

* duration per merge-flow state (mean seconds a PR spends in each state);
* the evidence-volume ratio — OCC evidence merges per non-OCC product merge —
  against the recorded baseline 1.67 and the target <=1.1;
* companions per merged product PR;
* same-head reruns bucketed by reason code;
* queue wait (total + p50 over transitions that entered the merge group);
* product failures found before vs after evidence was bound.

REPORT-ONLY: this module computes and reports. It does NOT gate, cap WIP, or
block a merge. The WIP cap (2 product lanes + 1 coverage lane) and any
auto-merge gate are deferred to a later enforcement PR that will consume these
metrics.
"""

from __future__ import annotations

from collections import defaultdict
from itertools import pairwise
from statistics import median

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.events.merge_state import (
    EnumMergeRerunReason,
    ModelMergeStateTransitionEvent,
)
from omnimarket.nodes.pr_ledger_native import EnumPrLifecyclePhase

# Recorded baseline and target for the evidence-volume ratio (OMN-14648). The
# baseline is the observed OCC-evidence-merges-per-product-merge as of the WS6
# ticket; the target is the value the merge-flow simplification work is driving
# toward. Kept as module constants so the enforcement PR can gate against the
# same numbers this report surfaces.
EVIDENCE_VOLUME_RATIO_BASELINE: float = 1.67
EVIDENCE_VOLUME_RATIO_TARGET: float = 1.1


class ModelMergeFlowMetrics(BaseModel):
    """Materialized merge-flow telemetry over a window of transitions."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    transitions_observed: int = Field(default=0, ge=0)

    mean_duration_seconds_per_state: dict[str, float] = Field(
        default_factory=dict,
        description="Mean wall-clock seconds a PR spent in each state before "
        "leaving it, keyed by EnumPrLifecyclePhase value.",
    )

    occ_evidence_merges: int = Field(default=0, ge=0)
    product_merges: int = Field(default=0, ge=0)
    evidence_volume_ratio: float | None = Field(
        default=None,
        description="occ_evidence_merges / product_merges. None when there are "
        "zero product merges (ratio undefined).",
    )
    evidence_volume_ratio_baseline: float = Field(
        default=EVIDENCE_VOLUME_RATIO_BASELINE
    )
    evidence_volume_ratio_target: float = Field(default=EVIDENCE_VOLUME_RATIO_TARGET)
    evidence_volume_meets_target: bool = Field(
        default=False,
        description="True iff the ratio is defined and <= the target (<=1.1).",
    )

    companions_per_product_pr: float | None = Field(
        default=None,
        description="Mean number of distinct OCC companion PRs bound per merged "
        "product PR. None when no product PRs were merged.",
    )

    same_head_reruns_by_reason: dict[str, int] = Field(
        default_factory=dict,
        description="Count of same-head rerun transitions bucketed by "
        "EnumMergeRerunReason value.",
    )

    queue_wait_seconds_total: float = Field(default=0.0, ge=0.0)
    queue_wait_seconds_p50: float | None = Field(
        default=None,
        description="Median queue wait over transitions that entered the merge "
        "group. None when there were none.",
    )

    product_failures_before_evidence: int = Field(default=0, ge=0)
    product_failures_after_evidence: int = Field(default=0, ge=0)


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def compute_merge_flow_metrics(
    transitions: list[ModelMergeStateTransitionEvent],
) -> ModelMergeFlowMetrics:
    """Fold merge-state transitions into materialized merge-flow metrics.

    Pure and deterministic: independent of input order except where the metric
    is intrinsically time-ordered (per-state durations sort by ``occurred_at``
    within each PR). No clocks, no I/O.
    """
    if not transitions:
        return ModelMergeFlowMetrics()

    # --- per-state durations -------------------------------------------------
    # Group transitions by PR, sort by time, and attribute the elapsed time
    # between consecutive transitions to the state the PR was IN (from_state of
    # the later transition == to_state of the earlier one).
    by_pr: dict[tuple[str, int], list[ModelMergeStateTransitionEvent]] = defaultdict(
        list
    )
    for t in transitions:
        by_pr[(t.repo, t.pr_number)].append(t)

    durations_by_state: dict[str, list[float]] = defaultdict(list)
    for events in by_pr.values():
        ordered = sorted(events, key=lambda e: e.occurred_at)
        for earlier, later in pairwise(ordered):
            state = earlier.to_state.value
            elapsed = (later.occurred_at - earlier.occurred_at).total_seconds()
            if elapsed >= 0:
                durations_by_state[state].append(elapsed)

    mean_duration_per_state = {
        state: _mean(vals) for state, vals in durations_by_state.items()
    }

    # --- evidence-volume ratio ----------------------------------------------
    # A "merge" is a transition into the TERMINAL sink whose PR reached the
    # merge group (i.e. actually landed). Count OCC-evidence merges vs product
    # merges. Dedupe on (repo, pr_number) so a PR that emits several terminal
    # rows is counted once.
    merged_prs: dict[tuple[str, int], bool] = {}  # key -> is_occ_evidence
    for t in transitions:
        if t.to_state is EnumPrLifecyclePhase.TERMINAL and t.from_state in (
            EnumPrLifecyclePhase.MERGE_GROUP,
            EnumPrLifecyclePhase.POST_MERGE_TAIL,
        ):
            merged_prs.setdefault((t.repo, t.pr_number), t.is_occ_evidence)

    occ_evidence_merges = sum(1 for is_occ in merged_prs.values() if is_occ)
    product_merges = sum(1 for is_occ in merged_prs.values() if not is_occ)
    evidence_volume_ratio: float | None = (
        occ_evidence_merges / product_merges if product_merges else None
    )
    evidence_volume_meets_target = (
        evidence_volume_ratio is not None
        and evidence_volume_ratio <= EVIDENCE_VOLUME_RATIO_TARGET
    )

    # --- companions per product PR ------------------------------------------
    # Count distinct OCC companion PRs bound to each product PR, over the whole
    # transition window (not just merged), then average across product PRs that
    # actually merged.
    companions_by_product: dict[tuple[str, int], set[int]] = defaultdict(set)
    for t in transitions:
        if t.is_occ_evidence and t.product_pr_number is not None:
            companions_by_product[(t.repo, t.product_pr_number)].add(t.pr_number)
    merged_product_keys = [k for k, is_occ in merged_prs.items() if not is_occ]
    companions_per_product_pr: float | None = None
    if merged_product_keys:
        companions_per_product_pr = _mean(
            [
                float(len(companions_by_product.get(k, set())))
                for k in merged_product_keys
            ]
        )

    # --- same-head reruns by reason -----------------------------------------
    reruns_by_reason: dict[str, int] = defaultdict(int)
    for t in transitions:
        if t.reason_code is not None:
            reruns_by_reason[t.reason_code.value] += 1
    # Deterministic key order for a stable projection payload.
    same_head_reruns_by_reason = {
        reason.value: reruns_by_reason[reason.value]
        for reason in EnumMergeRerunReason
        if reason.value in reruns_by_reason
    }

    # --- queue wait ----------------------------------------------------------
    queue_waits = [
        t.queue_wait_seconds
        for t in transitions
        if t.to_state is EnumPrLifecyclePhase.MERGE_GROUP
        and t.queue_wait_seconds is not None
    ]
    queue_wait_total = float(sum(queue_waits))
    queue_wait_p50 = float(median(queue_waits)) if queue_waits else None

    # --- product failures before vs after evidence --------------------------
    failures_before = sum(
        1 for t in transitions if t.product_failure_found and not t.evidence_present
    )
    failures_after = sum(
        1 for t in transitions if t.product_failure_found and t.evidence_present
    )

    return ModelMergeFlowMetrics(
        transitions_observed=len(transitions),
        mean_duration_seconds_per_state=mean_duration_per_state,
        occ_evidence_merges=occ_evidence_merges,
        product_merges=product_merges,
        evidence_volume_ratio=evidence_volume_ratio,
        evidence_volume_meets_target=evidence_volume_meets_target,
        companions_per_product_pr=companions_per_product_pr,
        same_head_reruns_by_reason=same_head_reruns_by_reason,
        queue_wait_seconds_total=queue_wait_total,
        queue_wait_seconds_p50=queue_wait_p50,
        product_failures_before_evidence=failures_before,
        product_failures_after_evidence=failures_after,
    )


__all__ = [
    "EVIDENCE_VOLUME_RATIO_BASELINE",
    "EVIDENCE_VOLUME_RATIO_TARGET",
    "ModelMergeFlowMetrics",
    "compute_merge_flow_metrics",
]
