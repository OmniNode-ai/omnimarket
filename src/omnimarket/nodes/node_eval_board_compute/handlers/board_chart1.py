# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Chart 1 of the Model tests tab: one line per model, a point per closed run's overall rate row.

A point is exactly one stored ``all`` rate row, never a combination. A change of model version,
prompt or settings, or scorer between two consecutive points of a line puts a marker there and
the line continues; a new task sample ends the line and starts a new one at a "new set of test
tasks" marker.
"""

from __future__ import annotations

from omnimarket.nodes.node_eval_board_compute.handlers.board_text import (
    SIZE_BAND_ALL,
    rate_of,
)
from omnimarket.nodes.node_eval_board_compute.models.model_eval_board_record import (
    EnumEvalBoardMarkerKind,
    ModelEvalBoardMarker,
    ModelEvalBoardPoint,
    ModelEvalBoardSeries,
)
from omnimarket.nodes.node_projection_eval_results.models.model_eval_rows import (
    EnumEvalRunState,
)
from omnimarket.nodes.node_projection_eval_results.models.model_eval_snapshot import (
    ModelEvalSnapshotModel,
    ModelEvalSnapshotRate,
    ModelEvalSnapshotRun,
)

LABEL_TASK_SET = "new set of test tasks"
LABEL_MODEL_VERSION = "new model version"
LABEL_PROMPT_OR_SETTINGS = "new prompt or settings"
LABEL_SCORER = "new way of checking"
NOTE_HOSTED = "The provider can change this model without a marker."


def closed_runs(model: ModelEvalSnapshotModel) -> list[ModelEvalSnapshotRun]:
    """The model's closed runs, oldest first. A void run draws no point."""
    runs = [r for r in model.runs if r.state is EnumEvalRunState.CLOSED]
    return sorted(runs, key=lambda r: (r.last_scored_at, r.run_id))


def _changes(
    before: ModelEvalSnapshotRun, after: ModelEvalSnapshotRun
) -> list[tuple[EnumEvalBoardMarkerKind, str]]:
    found: list[tuple[EnumEvalBoardMarkerKind, str]] = []
    if before.model_revision != after.model_revision:
        found.append((EnumEvalBoardMarkerKind.MODEL_VERSION, LABEL_MODEL_VERSION))
    if (
        before.generation_settings_hash != after.generation_settings_hash
        or before.prompt_template_version != after.prompt_template_version
    ):
        found.append(
            (EnumEvalBoardMarkerKind.PROMPT_OR_SETTINGS, LABEL_PROMPT_OR_SETTINGS)
        )
    if before.scorer_version != after.scorer_version:
        found.append((EnumEvalBoardMarkerKind.SCORER, LABEL_SCORER))
    return found


def build_series(
    label: str, model: ModelEvalSnapshotModel, min_n_point: int
) -> list[ModelEvalBoardSeries]:
    """The lines of one model: one per task sample, each point one overall rate row."""
    segments: list[list[ModelEvalSnapshotRun]] = []
    markers: list[list[tuple[int, EnumEvalBoardMarkerKind, str]]] = []
    previous: ModelEvalSnapshotRun | None = None
    for run in closed_runs(model):
        if overall_rate(run) is None:
            continue
        if previous is None or previous.task_sample_digest != run.task_sample_digest:
            segments.append([])
            kind, text = EnumEvalBoardMarkerKind.TASK_SET, LABEL_TASK_SET
            markers.append([(0, kind, text)] if previous is not None else [])
        else:
            index = len(segments[-1])
            markers[-1].extend(
                (index, kind, text) for kind, text in _changes(previous, run)
            )
        segments[-1].append(run)
        previous = run
    series: list[ModelEvalBoardSeries] = []
    for runs, marks in zip(segments, markers, strict=True):
        points: list[ModelEvalBoardPoint] = []
        for run in runs:
            row = overall_rate(run)
            assert row is not None
            rate = rate_of(row, min_n_point)
            points.append(
                ModelEvalBoardPoint(
                    tested_at=run.last_scored_at, rate=rate, hollow=rate.too_few
                )
            )
        series.append(
            ModelEvalBoardSeries(
                model_label=label,
                points=tuple(points),
                markers=tuple(
                    ModelEvalBoardMarker(
                        kind=kind,
                        point_index=index,
                        at=runs[index].last_scored_at,
                        label=text,
                    )
                    for index, kind, text in marks
                ),
                note=NOTE_HOSTED if runs[-1].engine not in ("", "local") else "",
            )
        )
    return series


def overall_rate(run: ModelEvalSnapshotRun) -> ModelEvalSnapshotRate | None:
    """The run's overall rate row: the one whose size label counts every band."""
    return next((r for r in run.rates if r.size_band == SIZE_BAND_ALL), None)


__all__ = [
    "LABEL_MODEL_VERSION",
    "LABEL_PROMPT_OR_SETTINGS",
    "LABEL_SCORER",
    "LABEL_TASK_SET",
    "NOTE_HOSTED",
    "build_series",
    "closed_runs",
    "overall_rate",
]
