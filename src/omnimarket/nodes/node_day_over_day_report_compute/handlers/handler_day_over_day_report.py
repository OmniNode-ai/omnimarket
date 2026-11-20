# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fold two day windows into the day-over-day report (OMN-26212). Pure: no clock, no I/O."""

from __future__ import annotations

from datetime import datetime

from omnimarket.models.model_day_over_day_report import (
    UNREADABLE,
    ModelBoardChange,
    ModelBoardChangeSection,
    ModelCostEntry,
    ModelCostReading,
    ModelCostSection,
    ModelDayOverDayReport,
    ModelDayOverDayRequest,
    ModelDayReadings,
    ModelDayRow,
    ModelFigure,
    ModelNewlyRunning,
    ModelNewlyRunningSection,
    ModelRegression,
    ModelRegressionSection,
    ModelThroughput,
)

_NO_SOURCE = "no source read"


def _figure(value: int | float | None, source: str, read_at: object) -> ModelFigure:
    at = read_at if isinstance(read_at, datetime) else None
    return ModelFigure(
        value=UNREADABLE if value is None else value,
        source=source or _NO_SOURCE,
        read_at=at,
    )


def _running_keys(day: ModelDayReadings) -> dict[str, tuple[str, str, object]] | None:
    if day.running is None:
        return None
    return {
        r.change: (r.host, r.source, r.read_at)
        for r in day.running
        if r.state == "running"
    }


class HandlerDayOverDayReport:
    """Handler for ``node_day_over_day_report_compute``."""

    def handle(self, request: ModelDayOverDayRequest) -> ModelDayOverDayReport:
        y, t = request.yesterday, request.today
        newly, stopped = self._running(y, t)
        board, reds = self._board(y, t)
        regress_readable = (
            "read" if newly.reading == "read" or board.reading == "read" else UNREADABLE
        )
        return ModelDayOverDayReport(
            yesterday_label=y.label,
            today_label=t.label,
            as_of=request.as_of,
            newly_running=newly,
            board_changes=board,
            throughput=self._throughput(y, t, request.as_of),
            cost_trend=self._cost(y, t),
            days=(self._day(y), self._day(t)),
            regressions=ModelRegressionSection(
                reading=regress_readable, entries=stopped + reds
            ),
        )

    @staticmethod
    def _day(d: ModelDayReadings) -> ModelDayRow:
        keys = _running_keys(d)
        tokens: int | None = None
        dollars: float | None = None
        if d.cost is not None:
            toks = [c.tokens for c in d.cost]
            tokens = (
                None
                if any(x is None for x in toks) or not toks
                else sum(x for x in toks if x is not None)
            )
            buck = [c.dollars for c in d.cost]
            dollars = (
                None
                if any(x is None for x in buck) or not buck
                else sum(x for x in buck if x is not None)
            )
        return ModelDayRow(
            date=d.label,
            merged=d.merges,
            seen_running=None if keys is None else len(keys),
            cost_usd=dollars,
            tokens=tokens,
        )

    @staticmethod
    def _running(
        y: ModelDayReadings, t: ModelDayReadings
    ) -> tuple[ModelNewlyRunningSection, tuple[ModelRegression, ...]]:
        before, after = _running_keys(y), _running_keys(t)
        if before is None or after is None:
            return ModelNewlyRunningSection(reading=UNREADABLE), ()
        newly = tuple(
            ModelNewlyRunning(change=c, host=h, source=s, read_at=at)
            for c, (h, s, at) in sorted(after.items())
            if c not in before
        )
        today_rows = {r.change: r for r in (t.running or ())}
        stopped = tuple(
            ModelRegression(
                kind="stopped_running",
                subject=c,
                detail=f"read RUNNING on {y.label} at {before[c][0]}; "
                f"{'read NOT RUNNING' if c in today_rows else 'not seen'} on {t.label}",
                source=today_rows[c].source if c in today_rows else before[c][1],
                read_at=today_rows[c].read_at if c in today_rows else before[c][2],
            )
            for c in sorted(before)
            if c not in after
        )
        return ModelNewlyRunningSection(reading="read", entries=newly), stopped

    @staticmethod
    def _board(
        y: ModelDayReadings, t: ModelDayReadings
    ) -> tuple[ModelBoardChangeSection, tuple[ModelRegression, ...]]:
        if y.board is None or t.board is None:
            return ModelBoardChangeSection(reading=UNREADABLE), ()
        before = {b.check_id: b for b in y.board}
        flips = []
        reds = []
        for b in sorted(t.board, key=lambda r: r.check_id):
            prior = before.get(b.check_id)
            if prior is None or prior.verdict == b.verdict:
                continue
            flips.append(
                ModelBoardChange(
                    check_id=b.check_id,
                    before=prior.verdict,
                    after=b.verdict,
                    source=b.source,
                    read_at=b.read_at,
                )
            )
            if b.verdict == "red":
                reds.append(
                    ModelRegression(
                        kind="board_red",
                        subject=b.check_id,
                        detail=f"green on {y.label}, red on {t.label}",
                        source=b.source,
                        read_at=b.read_at,
                    )
                )
        return ModelBoardChangeSection(reading="read", entries=tuple(flips)), tuple(
            reds
        )

    @staticmethod
    def _throughput(
        y: ModelDayReadings, t: ModelDayReadings, as_of: datetime
    ) -> ModelThroughput:
        def merges(d: ModelDayReadings) -> ModelFigure:
            return _figure(
                d.merges,
                d.merges_source or f"merges for {d.label}: {_NO_SOURCE}",
                as_of,
            )

        def running(d: ModelDayReadings) -> ModelFigure:
            keys = _running_keys(d)
            rows = d.running or ()
            return _figure(
                None if keys is None else len(keys),
                rows[0].source
                if rows
                else f"running readings for {d.label}: {_NO_SOURCE}",
                max((r.read_at for r in rows), default=None),
            )

        return ModelThroughput(
            merges_yesterday=merges(y),
            merges_today=merges(t),
            running_yesterday=running(y),
            running_today=running(t),
        )

    @staticmethod
    def _cost(y: ModelDayReadings, t: ModelDayReadings) -> ModelCostSection:
        if y.cost is None or t.cost is None:
            return ModelCostSection(reading=UNREADABLE)
        by_y = {c.engine: c for c in y.cost}
        by_t = {c.engine: c for c in t.cost}
        entries = []
        for engine in sorted(set(by_y) | set(by_t)):
            cy, ct = by_y.get(engine), by_t.get(engine)

            def fig(
                row: ModelCostReading | None,
                attr: str,
                day: ModelDayReadings,
                engine: str = engine,
            ) -> ModelFigure:
                if row is None:
                    # the day's ledger was read and holds no delegated= row for this engine: a counted zero
                    return _figure(
                        0 if attr == "calls" else None,
                        f"{engine} on {day.label}: no delegated= row in the ledger read",
                        None,
                    )
                return _figure(
                    getattr(row, attr),
                    row.source,
                    row.read_at,
                )

            entries.append(
                ModelCostEntry(
                    engine=engine,
                    calls_yesterday=fig(cy, "calls", y),
                    calls_today=fig(ct, "calls", t),
                    tokens_yesterday=fig(cy, "tokens", y),
                    tokens_today=fig(ct, "tokens", t),
                    dollars_yesterday=fig(cy, "dollars", y),
                    dollars_today=fig(ct, "dollars", t),
                )
            )
        return ModelCostSection(reading="read", entries=tuple(entries))
