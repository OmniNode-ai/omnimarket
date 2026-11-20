# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Build the compact snapshot from the rate rows and run records (OMN-24171, plan 5.2). Pure.

Only tracking runs reach the snapshot, for the models the overlay names, at most
``max_models``: each model's last ``snapshot_runs`` runs that read closed or void, and its last
``live_weeks`` live-work weeks. A run that is open or closing shows no rate. A run that is not a
tracking run stays in the tables. A text field over ``max_field_chars`` and a snapshot over
``snapshot_max_bytes`` are each refused with a typed error, never cut. Reads no clock.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from omnimarket.nodes.node_projection_eval_results.models.model_eval_rows import (
    SIZE_BAND_ALL,
    EnumEvalRunState,
    ModelEvalRateRow,
    ModelEvalRunRecord,
)
from omnimarket.nodes.node_projection_eval_results.models.model_eval_snapshot import (
    EnumEvalSnapshotRefusal,
    EvalSnapshotRefusedError,
    ModelEvalResultsOverlay,
    ModelEvalSnapshot,
    ModelEvalSnapshotLiveWeek,
    ModelEvalSnapshotModel,
    ModelEvalSnapshotRate,
    ModelEvalSnapshotRun,
)

TRACKING_SERIES = "tracking"
_BAND_ORDER = {"S": 0, "M": 1, "L": 2, SIZE_BAND_ALL: 3}


def _run_key(row: ModelEvalRateRow) -> tuple[str, ...]:
    return (
        row.model_id,
        row.run_id,
        row.task_set_digest,
        row.engine.value if row.engine else "",
        row.model_revision,
        row.generation_settings_hash,
        row.prompt_template_version,
        row.scorer_version,
        row.context_arm,
        str(row.replicate),
    )


def _tracking_runs(
    rows: list[ModelEvalRateRow], records: dict[str, ModelEvalRunRecord]
) -> dict[str, list[ModelEvalSnapshotRun]]:
    by_run: dict[tuple[str, ...], list[ModelEvalRateRow]] = defaultdict(list)
    for row in rows:
        if row.series == TRACKING_SERIES and row.run_state is not EnumEvalRunState.LIVE:
            by_run[_run_key(row)].append(row)
    runs: dict[str, list[ModelEvalSnapshotRun]] = defaultdict(list)
    covered: set[tuple[str, str]] = set()
    for group in by_run.values():
        head = group[0]
        state = head.run_state
        if state not in (EnumEvalRunState.CLOSED, EnumEvalRunState.VOID):
            continue
        covered.add((head.model_id, head.run_id))
        record = records.get(head.run_id)
        closed = state is EnumEvalRunState.CLOSED
        runs[head.model_id].append(
            ModelEvalSnapshotRun(
                run_id=head.run_id,
                state=state,
                series=head.series,
                engine=head.engine.value if head.engine else "",
                task_set_digest=head.task_set_digest,
                task_sample_digest=head.task_sample_digest,
                model_revision=head.model_revision,
                generation_settings_hash=head.generation_settings_hash,
                prompt_template_version=head.prompt_template_version,
                scorer_version=head.scorer_version,
                context_arm=head.context_arm,
                replicate=head.replicate,
                first_scored_at=min(r.first_scored_at for r in group)
                if closed
                else None,
                last_scored_at=(
                    max(r.last_scored_at for r in group)
                    if closed
                    else (
                        record.closed_at
                        if record
                        else max(r.last_scored_at for r in group)
                    )
                ),
                void_reason="" if closed else head.void_reason,
                band_usable=dict(head.band_usable),
                band_target=dict(head.band_target),
                rates=tuple(
                    ModelEvalSnapshotRate(
                        size_band=r.size_band,
                        n=r.n,
                        passed=r.passed,
                        unscorable_model=r.unscorable_model,
                        unscorable_infrastructure=r.unscorable_infrastructure,
                        wilson_low=r.wilson_low,
                        wilson_high=r.wilson_high,
                    )
                    for r in sorted(
                        group, key=lambda r: _BAND_ORDER.get(r.size_band, 9)
                    )
                )
                if closed
                else (),
            )
        )
    # A tracking run that closed void before any item arrived has no rate row: its record names it.
    for record in records.values():
        if record.series != TRACKING_SERIES or not record.void_reason:
            continue
        for model_id in sorted({s.model_id for s in record.sequences}):
            if (model_id, record.run_id) in covered:
                continue
            runs[model_id].append(
                ModelEvalSnapshotRun(
                    run_id=record.run_id,
                    state=EnumEvalRunState.VOID,
                    series=record.series,
                    task_sample_digest=record.task_sample_digest,
                    last_scored_at=record.closed_at,
                    void_reason=record.void_reason,
                    band_usable=dict(record.band_usable),
                    band_target=dict(record.band_target),
                )
            )
    return runs


def _live_weeks(
    rows: list[ModelEvalRateRow],
) -> dict[str, list[ModelEvalSnapshotLiveWeek]]:
    weeks: dict[str, list[ModelEvalSnapshotLiveWeek]] = defaultdict(list)
    for row in rows:
        if row.run_state is not EnumEvalRunState.LIVE or row.size_band != SIZE_BAND_ALL:
            continue
        weeks[row.model_id].append(
            ModelEvalSnapshotLiveWeek(
                week=row.run_id,
                n=row.n,
                passed=row.passed,
                wilson_low=row.wilson_low,
                wilson_high=row.wilson_high,
                first_scored_at=row.first_scored_at,
                last_scored_at=row.last_scored_at,
                reviewer_version_count=len(row.scorer_versions),
                reviewer_versions_digest=hashlib.sha256(
                    "\n".join(row.scorer_versions).encode("utf-8")
                ).hexdigest(),
            )
        )
    return weeks


def _check_field_lengths(node: Any, path: str, limit: int) -> None:
    if isinstance(node, str):
        if len(node) > limit:
            raise EvalSnapshotRefusedError(
                EnumEvalSnapshotRefusal.FIELD_TOO_LONG,
                f"{path} is {len(node)} characters, over the {limit} limit",
                size=len(node),
                limit=limit,
            )
    elif isinstance(node, dict):
        for key, value in node.items():
            _check_field_lengths(key, f"{path}.{key}", limit)
            _check_field_lengths(value, f"{path}.{key}", limit)
    elif isinstance(node, (list, tuple)):
        for index, value in enumerate(node):
            _check_field_lengths(value, f"{path}[{index}]", limit)


def build_snapshot(
    *,
    tenant_id: str,
    published_at: datetime,
    rates: Iterable[ModelEvalRateRow],
    records: Iterable[ModelEvalRunRecord],
    overlay: ModelEvalResultsOverlay,
) -> ModelEvalSnapshot:
    """The snapshot at ``published_at``, or a typed refusal. Run times come from the rows, never from ``published_at``."""
    rows = list(rates)
    runs = _tracking_runs(rows, {r.run_id: r for r in records})
    weeks = _live_weeks(rows)
    models: list[ModelEvalSnapshotModel] = []
    for model_id in list(overlay.display_names)[: overlay.max_models]:
        kept_runs = sorted(
            runs.get(model_id, ()),
            key=lambda r: (r.last_scored_at, r.run_id),
            reverse=True,
        )[: overlay.snapshot_runs]
        kept_weeks = sorted(
            weeks.get(model_id, ()), key=lambda w: w.week, reverse=True
        )[: overlay.live_weeks]
        models.append(
            ModelEvalSnapshotModel(
                model_id=model_id, runs=tuple(kept_runs), live_weeks=tuple(kept_weeks)
            )
        )
    snapshot = ModelEvalSnapshot(
        published_at=published_at, tenant_id=tenant_id, models=tuple(models)
    )
    _check_field_lengths(
        snapshot.model_dump(mode="json"), "snapshot", overlay.max_field_chars
    )
    size = len(snapshot.model_dump_json().encode("utf-8"))
    if size > overlay.snapshot_max_bytes:
        raise EvalSnapshotRefusedError(
            EnumEvalSnapshotRefusal.OVER_BOUND,
            f"snapshot is {size} bytes, over the {overlay.snapshot_max_bytes} bound",
            size=size,
            limit=overlay.snapshot_max_bytes,
        )
    return snapshot


__all__ = ["TRACKING_SERIES", "build_snapshot"]
