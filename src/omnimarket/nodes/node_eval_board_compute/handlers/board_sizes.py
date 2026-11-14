# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Chart 3 of the Model tests tab: the latest fixed test by size, and the largest reliable size.

The one judgment the board makes is a size cell's reliable or not, and it makes it with
``evaluate_range_line`` over the capability map's routing line, the function the capability map
folds with. A size is named only when it and every smaller size are reliable.
"""

from __future__ import annotations

from omnimarket.models.delegation_capability_map.model_capability_line import (
    ModelCapabilityLine,
)
from omnimarket.models.ranges import (
    EnumIncompleteRunTreatment,
    EnumRangeSampleOutcome,
    EnumRangeVerdict,
    ModelRangeAcceptanceLine,
    ModelRangeMethod,
    ModelRangeRun,
    ModelRangeSample,
)
from omnimarket.nodes.node_eval_board_compute.handlers.board_text import (
    SIZE_ORDER,
    SIZE_WORDS,
    percent_of,
    rate_of,
)
from omnimarket.nodes.node_eval_board_compute.models.model_eval_board_record import (
    EnumEvalBoardVerdict,
    ModelEvalBoardSizeCell,
    ModelEvalBoardSizeRow,
)
from omnimarket.nodes.node_projection_eval_results.models.model_eval_snapshot import (
    ModelEvalSnapshotRate,
    ModelEvalSnapshotRun,
)
from omnimarket.ranges import evaluate_range_line
from omnimarket.ranges.power import required_sample_size


def size_verdict(
    passed: int, n: int, line: ModelCapabilityLine, label: str
) -> EnumEvalBoardVerdict:
    """The routing line's verdict over ``n`` tasks of which ``passed`` passed."""
    required_n = required_sample_size(
        floor=line.floor,
        margin=line.margin,
        confidence=line.confidence,
        power=line.power,
    )
    acceptance = ModelRangeAcceptanceLine(
        check_id=f"size:{label}",
        case_set="the latest fixed test, one size",
        floor=line.floor,
        window="the latest fixed test",
        method=ModelRangeMethod(
            sample_size=required_n,
            confidence=line.confidence,
            power=line.power,
            margin=line.margin,
            incomplete_run_treatment=EnumIncompleteRunTreatment.COUNT_AS_FAILURE,
        ),
    )
    run = ModelRangeRun(
        run_id=label,
        samples=tuple(
            ModelRangeSample(
                case_id=f"task-{i}",
                outcome=(
                    EnumRangeSampleOutcome.PASS
                    if i < passed
                    else EnumRangeSampleOutcome.FAIL
                ),
            )
            for i in range(n)
        ),
    )
    verdict: EnumRangeVerdict = evaluate_range_line(acceptance, (run,)).verdict
    return EnumEvalBoardVerdict[verdict.name]


def _cell(
    band: str,
    row: ModelEvalSnapshotRate | None,
    line: ModelCapabilityLine,
    min_n_point: int,
    label: str,
) -> ModelEvalBoardSizeCell:
    if row is None or row.n == 0:
        return ModelEvalBoardSizeCell(
            size=SIZE_WORDS[band], rate=None, verdict=None, reliable=False
        )
    verdict = size_verdict(row.passed, row.n, line, f"{label}:{band}")
    return ModelEvalBoardSizeCell(
        size=SIZE_WORDS[band],
        rate=rate_of(row, min_n_point),
        verdict=verdict,
        reliable=verdict is EnumEvalBoardVerdict.MET and row.n >= min_n_point,
    )


def _reliable_text(named: list[str], floor_percent: int) -> str:
    if not named:
        return "no size is reliable yet"
    if len(named) == 1:
        both = f"{named[0]} tasks cleared {floor_percent}%"
    elif len(named) == 2:
        both = f"{named[0]} and {named[1]} tasks both cleared {floor_percent}%"
    else:
        both = f"{', '.join(named[:-1])} and {named[-1]} tasks all cleared {floor_percent}%"
    return f"reliable up to {named[-1]}: {both}"


def size_row(
    label: str,
    run: ModelEvalSnapshotRun,
    line: ModelCapabilityLine,
    min_n_point: int,
) -> ModelEvalBoardSizeRow:
    """One model's row for its latest closed run."""
    by_band = {r.size_band: r for r in run.rates}
    cells = tuple(
        _cell(band, by_band.get(band), line, min_n_point, label) for band in SIZE_ORDER
    )
    named: list[str] = []
    for cell in cells:
        if not cell.reliable:
            break
        named.append(cell.size)
    notes = tuple(
        f"not enough {SIZE_WORDS[band]} tasks yet "
        f"({run.band_usable.get(band, 0)} of {run.band_target[band]})"
        for band in SIZE_ORDER
        if band in run.band_target
        and run.band_usable.get(band, 0) < run.band_target[band]
    )
    return ModelEvalBoardSizeRow(
        model_label=label,
        tested_at=run.last_scored_at,
        cells=cells,
        reliable_up_to=named[-1] if named else None,
        reliable_text=_reliable_text(named, percent_of(line.floor)),
        short_band_notes=notes,
    )


__all__ = ["size_row", "size_verdict"]
