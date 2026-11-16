# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Recount a run's rates from the items held for it (OMN-24171, plan 5.2). Pure.

A rate is a recount of first tries, never an increment, so replay gives the same row: a task
with three tries counts once (its ordinal 1, at its latest outcome after any infrastructure
retry); an infrastructure-unscorable first try is outside ``n`` and counted by class; a
model-class one counts in ``n`` as not passed. A run that has a close reads closing, and shows
no rate on the snapshot, until every first try it closed over is held and none is still
infrastructure-unscorable.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

from omnimarket.enums.enum_eval_unscorable_reason import EnumEvalUnscorableClass
from omnimarket.handlers.stats_wilson import wilson_interval
from omnimarket.models.delegation.model_size_band import EnumSizeBand
from omnimarket.nodes.node_projection_eval_results.handlers.eval_results_mapping import (
    is_live_run,
)
from omnimarket.nodes.node_projection_eval_results.models.model_eval_rows import (
    SIZE_BAND_ALL,
    EnumEvalRunState,
    ModelEvalItemRow,
    ModelEvalRateRow,
    ModelEvalRunRecord,
)

SAMPLE_MISMATCH = "sample_mismatch"


def first_tries(items: Iterable[ModelEvalItemRow]) -> tuple[ModelEvalItemRow, ...]:
    """Each task's first try (ordinal 1), at its latest outcome: the one stored row of that ordinal."""
    return tuple(item for item in items if item.try_ordinal == 1)


def _infrastructure_unscorable(item: ModelEvalItemRow) -> bool:
    return (
        item.passed is None
        and item.unscorable_reason is not None
        and item.unscorable_reason.reason_class
        is EnumEvalUnscorableClass.INFRASTRUCTURE
    )


def _sample_state(
    record: ModelEvalRunRecord, tries: tuple[ModelEvalItemRow, ...]
) -> tuple[EnumEvalRunState, str]:
    """Whether the first tries held are the registered sequences: closed, still closing, or void.

    A close that names the context arm and replicate of every sequence is compared on the full
    (task, model, arm, replicate) key. A close that does not (dev's names only task and model) is
    compared per arm and replicate the held tries came from: each of them must hold every
    registered (task, model) and nothing else, so one arm's tries cannot stand for another's.
    A held key nobody registered voids the run; a registered key not yet held keeps it closing.
    """
    if all(s.context_arm for s in record.sequences):
        registered_full = {
            (s.task_id, s.model_id, s.context_arm, s.replicate)
            for s in record.sequences
        }
        held_full = {(t.task_id, t.model_id, t.context_arm, t.replicate) for t in tries}
        if held_full - registered_full:
            return EnumEvalRunState.VOID, SAMPLE_MISMATCH
        if registered_full - held_full:
            return EnumEvalRunState.CLOSING, ""
        return EnumEvalRunState.CLOSED, ""
    registered = {(s.task_id, s.model_id) for s in record.sequences}
    by_arm: dict[tuple[str, int], set[tuple[str, str]]] = defaultdict(set)
    for t in tries:
        by_arm[(t.context_arm, t.replicate)].add((t.task_id, t.model_id))
    if any(held - registered for held in by_arm.values()):
        return EnumEvalRunState.VOID, SAMPLE_MISMATCH
    if not by_arm or any(registered - held for held in by_arm.values()):
        return EnumEvalRunState.CLOSING, ""
    return EnumEvalRunState.CLOSED, ""


def run_state_of(
    run_id: str,
    record: ModelEvalRunRecord | None,
    tries: tuple[ModelEvalItemRow, ...],
) -> tuple[EnumEvalRunState, str]:
    """The run's state and its void cause (empty unless void)."""
    if is_live_run(run_id):
        return EnumEvalRunState.LIVE, ""
    if record is None:
        return EnumEvalRunState.OPEN, ""
    if record.void_reason:
        return EnumEvalRunState.VOID, record.void_reason
    if len(tries) < record.item_count or any(
        _infrastructure_unscorable(t) for t in tries
    ):
        return EnumEvalRunState.CLOSING, ""
    if record.sequences:
        return _sample_state(record, tries)
    return EnumEvalRunState.CLOSED, ""


def _group_key(item: ModelEvalItemRow, live: bool) -> tuple[str, ...]:
    if live:
        return (item.scorer.value, item.model_id)
    return (
        item.scorer.value,
        item.task_set_digest,
        item.engine.value,
        item.model_id,
        item.model_revision,
        item.generation_settings_hash,
        item.prompt_template_version,
        item.scorer_version,
        item.context_arm,
        str(item.replicate),
    )


def _count(items: list[ModelEvalItemRow]) -> tuple[int, int, int, int]:
    n = passed = unscorable_model = unscorable_infra = 0
    for item in items:
        if item.passed is None:
            reason = item.unscorable_reason
            if (
                reason is not None
                and reason.reason_class is EnumEvalUnscorableClass.INFRASTRUCTURE
            ):
                unscorable_infra += 1
                continue
            unscorable_model += 1
            n += 1
            continue
        n += 1
        passed += int(item.passed)
    return n, passed, unscorable_model, unscorable_infra


def recount_rates(
    *,
    tenant_id: str,
    run_id: str,
    items: Iterable[ModelEvalItemRow],
    record: ModelEvalRunRecord | None,
) -> tuple[ModelEvalRateRow, ...]:
    """Every rate row of the run over ``items``, in a stable order. Reads no clock."""
    tries = first_tries(items)
    state, void_reason = run_state_of(run_id, record, tries)
    live = state is EnumEvalRunState.LIVE
    groups: dict[tuple[str, ...], list[ModelEvalItemRow]] = defaultdict(list)
    for item in tries:
        groups[_group_key(item, live)].append(item)
    rows: list[ModelEvalRateRow] = []
    for key in sorted(groups):
        members = groups[key]
        head = members[0]
        bands: list[tuple[str, list[ModelEvalItemRow]]] = [(SIZE_BAND_ALL, members)]
        if not live:
            bands += [
                (band.value, [m for m in members if m.size_band is band])
                for band in EnumSizeBand
            ]
        for size_band, scoped in bands:
            if not scoped:
                continue
            n, passed, unscorable_model, unscorable_infra = _count(scoped)
            low, high = wilson_interval(passed, n)
            rows.append(
                ModelEvalRateRow(
                    tenant_id=tenant_id,
                    run_id=run_id,
                    scorer=head.scorer,
                    task_set_digest="" if live else head.task_set_digest,
                    engine=None if live else head.engine,
                    model_id=head.model_id,
                    model_revision="" if live else head.model_revision,
                    generation_settings_hash=""
                    if live
                    else head.generation_settings_hash,
                    prompt_template_version=""
                    if live
                    else head.prompt_template_version,
                    scorer_version="" if live else head.scorer_version,
                    context_arm="" if live else head.context_arm,
                    replicate=0 if live else head.replicate,
                    size_band=size_band,
                    n=n,
                    passed=passed,
                    unscorable_model=unscorable_model,
                    unscorable_infrastructure=unscorable_infra,
                    wilson_low=low,
                    wilson_high=high,
                    first_scored_at=min(m.scored_at for m in scoped),
                    last_scored_at=max(m.scored_at for m in scoped),
                    series=record.series if record else "",
                    run_state=state,
                    void_reason=void_reason,
                    task_sample_digest=record.task_sample_digest if record else "",
                    band_usable=dict(record.band_usable) if record else {},
                    band_target=dict(record.band_target) if record else {},
                    item_count=record.item_count if record else 0,
                    scorer_versions=(
                        tuple(sorted({m.scorer_version for m in scoped}))
                        if live
                        else ()
                    ),
                )
            )
    return tuple(rows)


__all__ = ["SAMPLE_MISMATCH", "first_tries", "recount_rates", "run_state_of"]
