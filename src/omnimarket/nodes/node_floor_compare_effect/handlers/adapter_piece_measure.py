# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Each live rebuild piece against the old way over its first 7 days (OMN-23081, AC1 and AC2).

``measure_pieces`` is pure: the compare history, the overlay, the clock and the verdicts already recorded in, one result
per declared piece out. The baseline is the mean of the measure's points in the ``baseline_days`` before the live date;
each trial day is the mean of its points; on day ``trial_days`` the trial mean is set against the baseline. Fewer than
``min_points`` points on either side reads unread, never a guess, and a verdict already recorded is returned unchanged.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from pathlib import Path

import yaml

from omnimarket.nodes.node_floor_compare_effect.models.model_floor_compare import (
    ModelSnapshot,
)
from omnimarket.nodes.node_floor_compare_effect.models.model_piece_measure import (
    MeasureVerdict,
    ModelMeasureDay,
    ModelMeasureDecl,
    ModelMeasureResult,
    ModelPieceMeasureDecl,
    ModelPieceMeasureResult,
    ModelPieceMeasuresOverlay,
    ModelPieceVerdictRecord,
    PieceVerdict,
)

NODE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_OVERLAY = NODE_DIR / "overlay.example.yaml"
OVERLAY_ENV = "ONEX_FLOOR_PIECE_MEASURES_OVERLAY"


def load_overlay(environ: Mapping[str, str]) -> ModelPieceMeasuresOverlay:
    """The overlay the env var names, else the node's shipped overlay."""
    named = environ.get(OVERLAY_ENV, "").strip()
    path = Path(named) if named else DEFAULT_OVERLAY
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return ModelPieceMeasuresOverlay.model_validate(raw["piece_measures"])


def read_verdicts(path: Path) -> dict[tuple[int, datetime], ModelPieceVerdictRecord]:
    out: dict[tuple[int, datetime], ModelPieceVerdictRecord] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            rec = ModelPieceVerdictRecord.model_validate(json.loads(line))
        except ValueError:
            continue
        out.setdefault((rec.piece, rec.live_at), rec)
    return out


def append_verdicts(path: Path, records: Iterable[ModelPieceVerdictRecord]) -> int:
    rows = [r.model_dump_json() for r in records]
    if not rows:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(row + "\n")
    return len(rows)


def _mean(
    snapshots: tuple[ModelSnapshot, ...], key: str, low: datetime, high: datetime
) -> tuple[float | None, int]:
    vals = [s.values[key] for s in snapshots if low <= s.at < high and key in s.values]
    return (sum(vals) / len(vals), len(vals)) if vals else (None, 0)


def _fmt(value: float | None, unit: str) -> str:
    if value is None:
        return "unread"
    return f"{round(value, 2):g} {unit}".rstrip()


def _measure_verdict(m: ModelMeasureDecl, base: float, trial: float) -> MeasureVerdict:
    if abs(trial - base) <= m.min_change * max(abs(base), 1.0):
        return "same"
    improved = (trial > base) == (m.polarity == "higher_is_better")
    return "better" if improved else "worse"


def _one_measure(
    m: ModelMeasureDecl,
    snapshots: tuple[ModelSnapshot, ...],
    live: datetime,
    now: datetime,
    ov: ModelPieceMeasuresOverlay,
    due: bool,
) -> ModelMeasureResult:
    b_from = live - timedelta(days=ov.baseline_days)
    base, base_n = _mean(snapshots, m.key, b_from, live)
    days: list[ModelMeasureDay] = []
    for d in range(1, ov.trial_days + 1):
        start = live + timedelta(days=d - 1)
        if start > now:
            break
        end = min(live + timedelta(days=d), now)
        value, n = _mean(snapshots, m.key, start, end + timedelta(microseconds=1))
        days.append(ModelMeasureDay(day=d, start=start, end=end, value=value, points=n))
    trial_end = min(live + timedelta(days=ov.trial_days), now)
    trial, trial_n = _mean(
        snapshots, m.key, live, trial_end + timedelta(microseconds=1)
    )
    verdict: MeasureVerdict | None = None
    base_text = f"baseline {_fmt(base, m.unit)} over {base_n} points in the {ov.baseline_days} days before it went live"
    trial_text = f"{_fmt(trial, m.unit)} over {trial_n} points since"
    if due:
        if base_n < ov.min_points or base is None:
            verdict = "unread"
            words = f"{m.label}: unread, the history holds {base_n} points before the live date (needs {ov.min_points}); {trial_text}."
        elif trial_n < ov.min_points or trial is None:
            verdict = "unread"
            words = f"{m.label}: unread, the history holds {trial_n} points in the {ov.trial_days} days since it went live (needs {ov.min_points}); {base_text}."
        else:
            verdict = _measure_verdict(m, base, trial)
            words = f"{m.label}: {trial_text}, {base_text}. {'Higher' if m.polarity == 'higher_is_better' else 'Lower'} is better, so this is {verdict}."
    else:
        words = f"{m.label}: {trial_text}; {base_text}. No verdict before day {ov.trial_days}."
    return ModelMeasureResult(
        key=m.key,
        label=m.label,
        unit=m.unit,
        polarity=m.polarity,
        source=m.source,
        baseline=base,
        baseline_points=base_n,
        baseline_from=b_from,
        baseline_to=live,
        days=tuple(days),
        trial=trial,
        trial_points=trial_n,
        verdict=verdict,
        words=words,
    )


def _piece_verdict(
    measures: tuple[ModelMeasureResult, ...],
) -> tuple[PieceVerdict, str]:
    """Unread if any measure is unread; else worse if any is worse; else better if any is better; all same reads unread."""
    v = [m.verdict for m in measures]
    unread = [m.label for m in measures if m.verdict == "unread"]
    if unread:
        return "unread", "unread measure: " + ", ".join(unread)
    worse = [m.label for m in measures if m.verdict == "worse"]
    if worse:
        return "worse", "worse than the old way: " + ", ".join(worse)
    if "better" in v:
        return "better", "better than the old way: " + ", ".join(
            m.label for m in measures if m.verdict == "better"
        )
    return "unread", "no measure moved from its baseline, so no difference was read"


def measure_piece(
    decl: ModelPieceMeasureDecl,
    snapshots: tuple[ModelSnapshot, ...],
    ov: ModelPieceMeasuresOverlay,
    now: datetime,
    recorded: ModelPieceVerdictRecord | None,
) -> ModelPieceMeasureResult:
    due_at = decl.live_at + timedelta(days=ov.trial_days)
    due = now >= due_at
    measures = tuple(
        _one_measure(m, snapshots, decl.live_at, now, ov, due) for m in decl.measures
    )
    elapsed = (now - decl.live_at).total_seconds()
    day = max(0, int(elapsed // 86400) + 1) if elapsed >= 0 else 0
    common = {
        "piece": decl.piece,
        "name": decl.name,
        "call": decl.call,
        "live_at": decl.live_at,
        "live_source": decl.live_source,
        "day": min(day, ov.trial_days) if due else day,
        "trial_days": ov.trial_days,
        "verdict_due": due_at,
        "measures": measures,
    }
    if recorded is not None:
        return ModelPieceMeasureResult(
            **common,
            verdict=recorded.verdict,
            verdict_at=recorded.verdict_at,
            recorded=True,
            reason=recorded.reason,
        )
    if not due:
        reason = (
            "not live yet"
            if day == 0
            else f"day {day} of {ov.trial_days}; the verdict is read on {due_at:%Y-%m-%d %H:%MZ}"
        )
        return ModelPieceMeasureResult(**common, verdict="pending", reason=reason)
    verdict, reason = _piece_verdict(measures)
    return ModelPieceMeasureResult(
        **common, verdict=verdict, verdict_at=now, reason=reason
    )


def measure_pieces(
    snapshots: tuple[ModelSnapshot, ...],
    ov: ModelPieceMeasuresOverlay,
    now: datetime,
    recorded: Mapping[tuple[int, datetime], ModelPieceVerdictRecord],
) -> tuple[tuple[ModelPieceMeasureResult, ...], tuple[ModelPieceVerdictRecord, ...]]:
    """Every declared piece's result, and the new day-7 verdicts to record (each recorded once)."""
    results: list[ModelPieceMeasureResult] = []
    fresh: list[ModelPieceVerdictRecord] = []
    for decl in ov.pieces:
        res = measure_piece(
            decl, snapshots, ov, now, recorded.get((decl.piece, decl.live_at))
        )
        results.append(res)
        if not res.recorded and res.verdict != "pending" and res.verdict_at is not None:
            fresh.append(
                ModelPieceVerdictRecord(
                    piece=res.piece,
                    live_at=res.live_at,
                    verdict=res.verdict,
                    verdict_at=res.verdict_at,
                    reason=res.reason,
                    measures={m.key: m.verdict or "unread" for m in res.measures},
                )
            )
    return tuple(results), tuple(fresh)
