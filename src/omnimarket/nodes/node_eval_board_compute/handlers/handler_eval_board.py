# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerEvalBoard: the projection's snapshot in, the Model tests tab's record out (OMN-24174).

Plan task T8. Pure and deterministic: no clock, no I/O. It computes no rate or range, since each
one is a stored rate row of the snapshot; the one judgment it owns is a size cell's reliable or
not, made with the range evaluator the capability map uses. Only the models the display overlay
names are drawn, in the overlay's order, up to its model cap.
"""

from __future__ import annotations

from omnimarket.nodes.node_eval_board_compute.handlers.board_chart1 import (
    build_series,
    latest_run,
    overall_rate,
)
from omnimarket.nodes.node_eval_board_compute.handlers.board_sizes import size_row
from omnimarket.nodes.node_eval_board_compute.handlers.board_text import (
    day,
    minute,
    plain,
    rate_of,
    week_date,
)
from omnimarket.nodes.node_eval_board_compute.models.model_eval_board_overlay import (
    GLOSSARY_TERMS,
    ModelEvalBoardOverlay,
)
from omnimarket.nodes.node_eval_board_compute.models.model_eval_board_record import (
    ModelEvalBoardChart1,
    ModelEvalBoardChart3,
    ModelEvalBoardDefinition,
    ModelEvalBoardFooter,
    ModelEvalBoardLiveRow,
    ModelEvalBoardLiveWork,
    ModelEvalBoardRecord,
    ModelEvalBoardSizeRow,
    ModelEvalBoardTested,
)
from omnimarket.nodes.node_eval_board_compute.models.model_eval_board_request import (
    ModelEvalBoardRequest,
)
from omnimarket.nodes.node_projection_eval_results.models.model_eval_rows import (
    EnumEvalRunState,
)
from omnimarket.nodes.node_projection_eval_results.models.model_eval_snapshot import (
    ModelEvalSnapshotModel,
)

CHART1_TITLE = "Are the models getting better?"
CHART1_ABOVE = (
    "The shaded band shows how sure we are of each test's own result, "
    "not whether a change between two tests is real."
)
CHART3_TITLE = "How big a task can each model handle?"
CHART3_ABOVE = "Each cell is the latest fixed test's result for tasks of that size."
CHART3_BELOW = (
    "A size turning reliable is a suggestion to send tasks of that size to this model "
    "by default; nothing changes until the operator approves it."
)
LIVE_TITLE = "How did the models do on the work they were actually sent?"
LIVE_ABOVE = (
    "This is the work models were actually sent that week, checked by a reviewer model; "
    "the mix of work changes, so a rise does not by itself mean a better model, and each "
    "model gets different work, so do not compare one model with another here."
)
CHECKS_TEXT = "Fixed tests are checked by running each task's tests; live work is checked by a reviewer model."


def _chart1_below(trend_look_points: int) -> str:
    return (
        f"A change of {trend_look_points} percentage points or more between two tests in a row "
        "(for example 60% to 70%), or any change across a marker, is worth a look."
    )


class HandlerEvalBoard:
    """COMPUTE handler: ``handle(request: ModelEvalBoardRequest) -> ModelEvalBoardRecord``."""

    def handle(self, request: ModelEvalBoardRequest) -> ModelEvalBoardRecord:
        overlay = request.overlay
        snapshot = request.snapshot
        held = {m.model_id: m for m in snapshot.models}
        shown: list[tuple[str, ModelEvalSnapshotModel]] = [
            (label, held[model_id])
            for model_id, label in overlay.display_names.items()
            if model_id in held
        ][: overlay.max_models]

        series = []
        rows: list[ModelEvalBoardSizeRow] = []
        live_rows: list[ModelEvalBoardLiveRow] = []
        clauses: list[str] = []
        tested: list[ModelEvalBoardTested] = []
        void_notes: list[str] = []
        for label, model in shown:
            series.extend(build_series(label, model, overlay.min_n_point))
            latest = latest_run(model)
            if latest is not None:
                rows.append(
                    size_row(label, latest, overlay.size_line, overlay.min_n_point)
                )
                overall = overall_rate(latest)
                if overall is not None:
                    clauses.append(
                        f"{label} {rate_of(overall, overlay.min_n_point).text}"
                    )
                tested.append(
                    ModelEvalBoardTested(
                        model_label=label,
                        tested_at=latest.last_scored_at,
                        text=f"{label} last tested {day(latest.last_scored_at)}.",
                    )
                )
            else:
                tested.append(
                    ModelEvalBoardTested(
                        model_label=label,
                        tested_at=None,
                        text=f"{label} has no finished test yet.",
                    )
                )
            for run in sorted(model.runs, key=lambda r: (r.last_scored_at, r.run_id)):
                if run.state is EnumEvalRunState.VOID:
                    cause = plain(run.void_reason) or "no cause given"
                    void_notes.append(
                        f"{label}'s test on {day(run.last_scored_at)} was not counted: {cause}."
                    )
            live_rows.extend(
                ModelEvalBoardLiveRow(
                    model_label=label,
                    week=week_date(week),
                    rate=rate_of(week, overlay.min_n_point),
                )
                for week in sorted(model.live_weeks, key=lambda w: w.week)
            )
        return ModelEvalBoardRecord(
            headline=_headline(clauses),
            chart1=ModelEvalBoardChart1(
                title=CHART1_TITLE,
                above=CHART1_ABOVE,
                below=_chart1_below(overlay.trend_look_points),
                series=tuple(series),
            ),
            chart3=ModelEvalBoardChart3(
                title=CHART3_TITLE,
                above=CHART3_ABOVE,
                below=CHART3_BELOW,
                rows=tuple(rows),
            ),
            live_work=ModelEvalBoardLiveWork(
                title=LIVE_TITLE, above=LIVE_ABOVE, rows=tuple(live_rows)
            ),
            glossary=_glossary(overlay),
            footer=ModelEvalBoardFooter(
                published_at=snapshot.published_at,
                published_text=f"Page data published {minute(snapshot.published_at)} UTC.",
                last_tested=tuple(tested),
                void_notes=tuple(void_notes),
                checks_text=CHECKS_TEXT,
            ),
        )


def _headline(clauses: list[str]) -> str:
    if not clauses:
        return "No model has a finished weekly test yet."
    return f"In the latest weekly test, {'; '.join(clauses)}."


def _glossary(overlay: ModelEvalBoardOverlay) -> tuple[ModelEvalBoardDefinition, ...]:
    return tuple(
        ModelEvalBoardDefinition(term=term, definition=overlay.glossary[term])
        for term in GLOSSARY_TERMS
    )


__all__ = ["HandlerEvalBoard"]
