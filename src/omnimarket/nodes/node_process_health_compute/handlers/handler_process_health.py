# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The process-health series: one metric list per UTC day and the before and after summary."""

from __future__ import annotations

from datetime import datetime, timedelta

from omnimarket.models.process_health import (
    EnumSourceName,
    ModelDaySeries,
    ModelProcessHealthRequest,
    ModelProcessHealthResult,
    ModelSourceReport,
    ModelWindowMetrics,
)
from omnimarket.nodes.node_process_health_compute.handlers.calc_landing import (
    landing_metrics,
)
from omnimarket.nodes.node_process_health_compute.handlers.calc_ledger import (
    ledger_metrics,
)
from omnimarket.nodes.node_process_health_compute.handlers.calc_repo import (
    repo_metrics,
)
from omnimarket.nodes.node_process_health_compute.handlers.context import (
    Context,
    build_context,
)
from omnimarket.nodes.node_process_health_compute.handlers.metric_builder import (
    round_value,
)
from omnimarket.nodes.node_process_health_compute.handlers.summary import (
    WindowCalculator,
    build_summary,
)
from omnimarket.nodes.node_process_health_compute.handlers.window import Window

SCHEMA_VERSION = 1
_ONE_DAY = timedelta(days=1)


def _midnight(moment: datetime) -> datetime:
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)


class _Calculator(WindowCalculator):
    def __init__(self, ctx: Context) -> None:
        self._ctx = ctx

    def __call__(self, window: Window) -> ModelWindowMetrics:
        metrics = [
            *ledger_metrics(self._ctx, window),
            *repo_metrics(self._ctx, window),
            *landing_metrics(self._ctx, window),
        ]
        return ModelWindowMetrics(
            start=window.start,
            end=window.end,
            hours=round_value(window.hours),
            metrics=tuple(sorted(metrics, key=lambda metric: metric.key)),
        )


def _record_counts(request: ModelProcessHealthRequest) -> dict[EnumSourceName, int]:
    return {
        EnumSourceName.LEDGER: len(request.ledger_rows),
        EnumSourceName.REPO_STATUS_EVENTS: len(request.repo_status_events),
        EnumSourceName.GIT_MERGES: len(request.merges),
        EnumSourceName.LANDING_DECISIONS: len(request.decisions),
        EnumSourceName.LANDING_COST: len(request.leases),
        EnumSourceName.LANDING_DELEGATION: len(request.delegation_rows),
        EnumSourceName.LANDING_METRICS_HISTORY: len(request.landing_metrics),
        EnumSourceName.LINEAR_SNAPSHOT: len(request.linear_issues),
    }


def _source_reports(
    request: ModelProcessHealthRequest,
) -> tuple[ModelSourceReport, ...]:
    counts = _record_counts(request)
    declared = {source.name: source for source in request.sources}
    reports: list[ModelSourceReport] = []
    for name in EnumSourceName:
        source = declared.get(name)
        if source is None:
            continue
        gap = sum((g.end - g.start).total_seconds() for g in source.gaps) / 3600.0
        reports.append(
            ModelSourceReport(
                name=name,
                status=source.status,
                locator=source.locator,
                reason=source.reason,
                covered_from=source.covered_from,
                covered_to=source.covered_to,
                gap_hours=round_value(gap),
                records=counts[name],
            )
        )
    return tuple(reports)


class HandlerProcessHealth:
    """Pure: the same request gives the same result, with no file, network or clock read."""

    def handle(self, request: ModelProcessHealthRequest) -> ModelProcessHealthResult:
        ctx = build_context(request)
        calculator = _Calculator(ctx)
        config = request.config
        last_day = _midnight(request.generated_at - timedelta(microseconds=1))
        days: list[ModelDaySeries] = []
        for offset in range(config.series_days - 1, -1, -1):
            start = last_day - offset * _ONE_DAY
            end = min(start + _ONE_DAY, request.generated_at)
            if end <= start:
                continue
            days.append(
                ModelDaySeries(
                    date=start.strftime("%Y-%m-%d"),
                    is_reset_day=start <= config.reset_at < start + _ONE_DAY,
                    partial=end < start + _ONE_DAY,
                    window=calculator(Window(start, end)),
                )
            )
        return ModelProcessHealthResult(
            schema_version=SCHEMA_VERSION,
            generated_at=request.generated_at,
            reset_at=config.reset_at,
            reset_label=config.reset_label,
            sources=_source_reports(request),
            days=tuple(days),
            summary=build_summary(ctx, calculator),
        )
