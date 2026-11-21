# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerFloorCostThroughput: the cost-and-throughput panel as a pure fold.

Definition-B compute: ``handle(request: ModelCostThroughputRequest) -> ModelCostThroughputPanel``. No file, no clock, no network, no
model. The readings come from the effect node; this fold only reads what they hold. A figure whose source is missing, unread or
incomplete is UNREADABLE with the reason, never 0 and never blank. A dollar figure is a sum of recorded costs or UNREADABLE: one
attempt with no recorded cost makes the engine's dollars and the cost per change UNREADABLE, because an estimate is not a reading.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from omnimarket.models.floor_cost_throughput import (
    ENGINES,
    SCHEMA_ID,
    TREND_DAYS,
    UNREADABLE,
    ModelCostThroughputPanel,
    ModelCostThroughputRequest,
    ModelDayOverDayDay,
    ModelDayOverDayReport,
    ModelEngineRow,
    ModelFigure,
    ModelFigureSource,
    ModelSeenRunningPoint,
    ModelSeenRunningSeries,
    ModelSourceDocument,
    ModelTrend,
    ModelTrendDay,
)

_STAMP = "%Y-%m-%dT%H:%M:%SZ"
_WINDOW = timedelta(hours=24)
# the readings must span (almost) the whole window before a per-change cost is drawn from them
_MIN_SPAN_FOR_COST = timedelta(hours=23)
_MIN_SPAN_FOR_RATE = timedelta(minutes=30)

# The sources a figure names when its reading was never made.
FLOOR_SIMPLE_NODE = "node_floor_simple_effect"
DELEGATION_NODE = "node_floor_delegation_history_compute"
DAY_OVER_DAY_NODE = "node_day_over_day_report_compute"

Format = Callable[[float], str]


def _parse(stamp: object) -> datetime | None:
    if not isinstance(stamp, str):
        return None
    try:
        return datetime.strptime(stamp, _STAMP).replace(tzinfo=UTC)
    except ValueError:
        return None


def _count(value: float) -> str:
    return f"{value:g}"


def _rate(value: float) -> str:
    return f"{value:.2f}"


def _dollars(value: float) -> str:
    return f"${value:.2f}"


def _dollars_small(value: float) -> str:
    return f"${value:.4f}"


def _figure(
    key: str,
    label: str,
    unit: str,
    source: ModelFigureSource,
    value: float | None,
    reason: str,
    fmt: Format = _count,
) -> ModelFigure:
    if value is None:
        return ModelFigure(
            key=key,
            label=label,
            unit=unit,
            value=None,
            display=UNREADABLE,
            source=source,
            reason=reason or "the source was not read",
        )
    return ModelFigure(
        key=key, label=label, unit=unit, value=value, display=fmt(value), source=source
    )


def _unread(
    key: str, label: str, unit: str, node: str, data_read: str, reason: str
) -> ModelFigure:
    return _figure(
        key,
        label,
        unit,
        ModelFigureSource(node=node, data_read=data_read),
        None,
        reason,
    )


# ---- merges per hour -------------------------------------------------------------------------------------------------------

_MERGES_READ = "floor-simple.json numbers[merges]"


def _merges_per_hour(doc: ModelSourceDocument | None) -> ModelFigure:
    key, label, unit = "merges_per_hour", "Merges in the last hour", "merges per hour"
    if doc is None:
        return _unread(
            key,
            label,
            unit,
            FLOOR_SIMPLE_NODE,
            _MERGES_READ,
            "the simple floor document was not read",
        )
    source = ModelFigureSource(
        node=doc.node, data_read=doc.data_read, read_at=doc.read_at
    )
    numbers = doc.document.get("numbers")
    entry = (
        next(
            (n for n in numbers if isinstance(n, dict) and n.get("key") == "merges"),
            None,
        )
        if isinstance(numbers, list)
        else None
    )
    if entry is None:
        return _figure(
            key,
            label,
            unit,
            source,
            None,
            "the simple floor document carries no merges number",
        )
    entry_source = ModelFigureSource(
        node=doc.node,
        data_read=doc.data_read,
        read_at=str(entry.get("read_at") or doc.read_at),
    )
    if entry.get("read") is not True:
        return _figure(
            key,
            label,
            unit,
            entry_source,
            None,
            "the simple floor did not read its merges number",
        )
    try:
        value = float(str(entry.get("value")).strip())
    except ValueError:
        return _figure(
            key, label, unit, entry_source, None, "the merges number is not a number"
        )
    return _figure(key, label, unit, entry_source, value, "")


# ---- changes seen running per hour ----------------------------------------------------------------------------------------


def _window_points(
    series: ModelSeenRunningSeries, now: datetime
) -> list[tuple[datetime, ModelSeenRunningPoint]]:
    points: list[tuple[datetime, ModelSeenRunningPoint]] = []
    for point in series.points:
        stamp = _parse(point.read_at)
        if stamp is not None and now - _WINDOW <= stamp <= now:
            points.append((stamp, point))
    points.sort(key=lambda item: item[0])
    return points


def _seen_running(
    series: ModelSeenRunningSeries | None, now: datetime | None
) -> tuple[ModelFigure, float | None, timedelta | None]:
    """The figure, the count of changes seen running over the readings' span and the span."""
    key, label, unit = (
        "seen_running_per_hour",
        "Changes seen running per hour",
        "changes per hour",
    )
    node, data_read = (
        (series.node, series.data_read)
        if series
        else (FLOOR_SIMPLE_NODE, "floor-simple.json integration.counts.seen_running")
    )
    if series is None:
        return (
            _unread(
                key,
                label,
                unit,
                node,
                data_read,
                "no readings of the integration record were kept",
            ),
            None,
            None,
        )
    if now is None:
        return (
            _unread(
                key, label, unit, node, data_read, "the fold's clock is not a UTC stamp"
            ),
            None,
            None,
        )
    points = _window_points(series, now)
    if len(points) < 2:
        return (
            _unread(
                key,
                label,
                unit,
                node,
                data_read,
                "fewer than two readings of the integration record in the last 24 hours",
            ),
            None,
            None,
        )
    (first_at, first), (last_at, last) = points[0], points[-1]
    source = ModelFigureSource(node=node, data_read=data_read, read_at=last.read_at)
    span = last_at - first_at
    if span < _MIN_SPAN_FOR_RATE:
        return (
            _figure(
                key,
                label,
                unit,
                source,
                None,
                "the kept readings are less than 30 minutes apart",
            ),
            None,
            None,
        )
    delta = last.seen_running - first.seen_running
    if delta < 0:
        return (
            _figure(
                key,
                label,
                unit,
                source,
                None,
                "the seen-running count fell between the kept readings",
            ),
            None,
            None,
        )
    hours = span.total_seconds() / 3600
    return (
        _figure(key, label, unit, source, delta / hours, "", _rate),
        float(delta),
        span,
    )


# ---- delegation calls and dollars by engine -------------------------------------------------------------------------------

_DELEGATION_READ = "delegation-history.json models and series[task_kind=*].hours"
_NO_TOKENS = "the delegation history records attempts and cost, not tokens; no token reading was made"


class _EngineReading:
    """One engine's calls and recorded dollars over the window; ``dollars`` is None when any attempt had no recorded cost."""

    def __init__(self) -> None:
        self.calls = 0
        self.dollars: float | None = 0.0
        self.unpriced_model = ""


def _read_delegation(
    doc: ModelSourceDocument, now: datetime
) -> tuple[dict[str, _EngineReading], str] | str:
    """The readings per engine kind, plus the unpriced model of any other engine; or the reason the document cannot be read."""
    models = doc.document.get("models")
    series = doc.document.get("series")
    if not isinstance(models, list) or not isinstance(series, list):
        return "the delegation history carries no models or series"
    kind_of = {
        str(m.get("id")): str(m.get("engine_kind"))
        for m in models
        if isinstance(m, dict)
    }
    readings: dict[str, _EngineReading] = {
        kind: _EngineReading() for kind in (*ENGINES, "other")
    }
    for entry in series:
        if not isinstance(entry, dict) or entry.get("task_kind") != "*":
            continue
        model = str(entry.get("model"))
        kind = kind_of.get(model, "other")
        reading = readings.setdefault(kind, _EngineReading())
        hours = entry.get("hours")
        if not isinstance(hours, list):
            return f"the series of {model} carries no hourly points"
        for point in hours:
            if not isinstance(point, dict):
                continue
            bucket = _parse(point.get("bucket"))
            attempts = point.get("attempts")
            if (
                bucket is None
                or not isinstance(attempts, int)
                or not now - _WINDOW < bucket <= now
            ):
                continue
            reading.calls += attempts
            cost = point.get("cost_usd")
            if attempts > 0 and not isinstance(cost, int | float):
                reading.dollars = None
                reading.unpriced_model = reading.unpriced_model or model
            elif reading.dollars is not None and isinstance(cost, int | float):
                reading.dollars += float(cost)
    return readings, ""


def _engine_rows(
    doc: ModelSourceDocument | None, now: datetime | None
) -> tuple[tuple[ModelEngineRow, ...], float | None, str]:
    """The four engine rows, the total recorded dollars (None when not all recorded) and, when None, why."""
    rows: list[ModelEngineRow] = []
    node = doc.node if doc else DELEGATION_NODE
    data_read = doc.data_read if doc else _DELEGATION_READ
    read_at = doc.read_at if doc else None
    source = ModelFigureSource(node=node, data_read=data_read, read_at=read_at)
    missing = ""
    readings: dict[str, _EngineReading] = {}
    if doc is None:
        missing = "the delegation history was not read"
    elif now is None:
        missing = "the fold's clock is not a UTC stamp"
    else:
        found = _read_delegation(doc, now)
        if isinstance(found, str):
            missing = found
        else:
            readings = found[0]
    total: float | None = 0.0
    why = ""
    for engine in ENGINES:
        calls_key, tokens_key, dollars_key = (
            f"{engine}_calls",
            f"{engine}_tokens",
            f"{engine}_dollars",
        )
        label = f"{engine} delegation"
        tokens = _figure(
            tokens_key, f"{label} tokens", "tokens", source, None, _NO_TOKENS
        )
        if missing:
            rows.append(
                ModelEngineRow(
                    engine=engine,
                    calls=_figure(
                        calls_key,
                        f"{label} calls, last 24 h",
                        "calls",
                        source,
                        None,
                        missing,
                    ),
                    tokens=tokens,
                    dollars=_figure(
                        dollars_key,
                        f"{label} dollars, last 24 h",
                        "usd",
                        source,
                        None,
                        missing,
                        _dollars,
                    ),
                )
            )
            total, why = None, missing
            continue
        reading = readings[engine]
        calls = _figure(
            calls_key,
            f"{label} calls, last 24 h",
            "calls",
            source,
            float(reading.calls),
            "",
        )
        if reading.dollars is None:
            reason = f"an attempt of {reading.unpriced_model} has no recorded cost"
            dollars = _figure(
                dollars_key,
                f"{label} dollars, last 24 h",
                "usd",
                source,
                None,
                reason,
                _dollars,
            )
            total, why = None, why or reason
        elif reading.calls == 0:
            reason = "no calls in the last 24 hours, so no cost was recorded"
            dollars = _figure(
                dollars_key,
                f"{label} dollars, last 24 h",
                "usd",
                source,
                None,
                reason,
                _dollars,
            )
            total, why = None, why or reason
        else:
            dollars = _figure(
                dollars_key,
                f"{label} dollars, last 24 h",
                "usd",
                source,
                reading.dollars,
                "",
                _dollars,
            )
            if total is not None:
                total += reading.dollars
        rows.append(
            ModelEngineRow(engine=engine, calls=calls, tokens=tokens, dollars=dollars)
        )
    other = readings.get("other")
    if total is not None and other is not None and other.dollars is None:
        total, why = (
            None,
            f"an attempt of {other.unpriced_model} (an engine outside the four) has no recorded cost",
        )
    elif total is not None and other is not None and other.dollars is not None:
        total += other.dollars
    return tuple(rows), total, why


# ---- cost per change seen running ------------------------------------------------------------------------------------------


def _cost_per_change(
    doc: ModelSourceDocument | None,
    series: ModelSeenRunningSeries | None,
    dollars: float | None,
    dollars_why: str,
    delta: float | None,
    span: timedelta | None,
    seen_reason: str,
) -> ModelFigure:
    key, label, unit = (
        "cost_per_change_seen_running",
        "Cost per change seen running, last 24 h",
        "usd per change",
    )
    node = f"{doc.node if doc else DELEGATION_NODE} + {series.node if series else FLOOR_SIMPLE_NODE}"
    data_read = f"{doc.data_read if doc else _DELEGATION_READ}; {series.data_read if series else 'integration.counts.seen_running'}"
    source = ModelFigureSource(
        node=node, data_read=data_read, read_at=doc.read_at if doc else None
    )
    if dollars is None:
        return _figure(
            key,
            label,
            unit,
            source,
            None,
            f"the dollar total is not recorded: {dollars_why}",
        )
    if delta is None or span is None:
        return _figure(
            key,
            label,
            unit,
            source,
            None,
            f"the changes seen running are not read: {seen_reason}",
        )
    if span < _MIN_SPAN_FOR_COST:
        return _figure(
            key,
            label,
            unit,
            source,
            None,
            "the kept readings of the integration record span less than 24 hours",
        )
    if delta <= 0:
        return _figure(
            key, label, unit, source, None, "no change was seen running in the window"
        )
    return _figure(key, label, unit, source, dollars / delta, "", _dollars_small)


# ---- the 7-day trend ---------------------------------------------------------------------------------------------------------


def _trend(report: ModelDayOverDayReport | None, now: datetime | None) -> ModelTrend:
    node = report.node if report else DAY_OVER_DAY_NODE
    data_read = "the day-over-day report's days (merged, seen running, cost)"
    source = ModelFigureSource(
        node=node, data_read=data_read, read_at=report.generated_at if report else None
    )
    if now is None:
        dates: list[str] = [f"day-{n}" for n in range(TREND_DAYS)]
    else:
        dates = [
            (now.date() - timedelta(days=TREND_DAYS - 1 - n)).isoformat()
            for n in range(TREND_DAYS)
        ]
    by_date: dict[str, ModelDayOverDayDay] = (
        {d.date: d for d in report.days} if report else {}
    )
    absent = (
        "no day-over-day report was read (its node has not landed or wrote nothing)"
    )
    days: list[ModelTrendDay] = []
    for n, date in enumerate(dates):
        day = by_date.get(date)
        cells: list[ModelFigure] = []
        for what, text, unit, fmt, value in (
            ("merged", "Merged", "merges", _count, day.merged if day else None),
            (
                "seen_running",
                "Seen running",
                "changes",
                _count,
                day.seen_running if day else None,
            ),
            ("cost_usd", "Cost", "usd", _dollars, day.cost_usd if day else None),
        ):
            reason = (
                absent
                if report is None
                else f"the day-over-day report has no {what} for {date}"
            )
            cells.append(
                _figure(
                    f"trend_{n}_{what}",
                    f"{text} on {date}",
                    unit,
                    source,
                    None if value is None else float(value),
                    reason,
                    fmt,
                )
            )
        days.append(
            ModelTrendDay(
                date=date, merged=cells[0], seen_running=cells[1], cost_usd=cells[2]
            )
        )
    read = report is not None and any(d.date in by_date for d in days)
    reason = (
        ""
        if read
        else (
            absent
            if report is None
            else "the day-over-day report has none of the last 7 days"
        )
    )
    return ModelTrend(
        status="read" if read else UNREADABLE,
        reason=reason,
        source=source,
        days=tuple(days),
    )


# ---- the fold ----------------------------------------------------------------------------------------------------------------


class HandlerFloorCostThroughput:
    """``handle(request: ModelCostThroughputRequest) -> ModelCostThroughputPanel``."""

    def handle(self, request: ModelCostThroughputRequest) -> ModelCostThroughputPanel:
        now = _parse(request.generated_at)
        merges = _merges_per_hour(request.floor_simple)
        seen, delta, span = _seen_running(request.seen_running_series, now)
        rows, dollars, dollars_why = _engine_rows(request.delegation_history, now)
        per_change = _cost_per_change(
            request.delegation_history,
            request.seen_running_series,
            dollars,
            dollars_why,
            delta,
            span,
            seen.reason,
        )
        trend = _trend(request.day_over_day, now)
        figures: list[ModelFigure] = [merges, seen, per_change]
        for row in rows:
            figures.extend((row.calls, row.tokens, row.dollars))
        for day in trend.days:
            figures.extend((day.merged, day.seen_running, day.cost_usd))
        unread = [f for f in figures if f.value is None]
        reasons: list[str] = []
        for figure in unread:
            if figure.reason not in reasons:
                reasons.append(figure.reason)
        return ModelCostThroughputPanel(
            schema_id=SCHEMA_ID,
            generated_at=request.generated_at,
            merges_per_hour=merges,
            seen_running_per_hour=seen,
            engines=rows,
            cost_per_change_seen_running=per_change,
            trend=trend,
            unreadable_count=len(unread),
            unreadable_reasons=tuple(reasons),
        )
