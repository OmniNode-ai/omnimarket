# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fold lab indicator verdicts into history and render the board page.

Canonical definition-B shape: ``handle(ModelLabSliBoardRequest) ->
ModelLabSliBoard``. Pure: the projection comes in on the request and goes out
on the result.
"""

from __future__ import annotations

import html
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.enums.enum_lab_sli import EnumLabSliSeverity, EnumLabSliStatus
from omnimarket.models.lab_sli import (
    ModelLabSliBoard,
    ModelLabSliBoardRequest,
    ModelLabSliEvaluated,
    ModelLabSliHistoryPoint,
    ModelLabSliProjection,
)
from omnimarket.nodes.contract_topics import contract_section

CONTRACT_PATH = Path(__file__).resolve().parent.parent / "contract.yaml"
_SPARK = "▁▂▃▄▅▆▇█"
_STATUS_ORDER = {
    EnumLabSliStatus.BREACH: 0,
    EnumLabSliStatus.UNKNOWN: 1,
    EnumLabSliStatus.OK: 2,
    EnumLabSliStatus.NOT_APPLICABLE: 3,
}
_STATUS_MARK = {
    EnumLabSliStatus.BREACH: "x",
    EnumLabSliStatus.UNKNOWN: "?",
    EnumLabSliStatus.OK: ".",
    EnumLabSliStatus.NOT_APPLICABLE: "-",
}


class ModelLabSliBoardConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    history_points_per_indicator: int = Field(ge=1)
    trend_points: int = Field(ge=1)


def load_board_config(path: Path = CONTRACT_PATH) -> ModelLabSliBoardConfig:
    block = contract_section(path, "config", "lab_sli_board")
    return ModelLabSliBoardConfig.model_validate(block)


@lru_cache(maxsize=1)
def _contract_config() -> ModelLabSliBoardConfig:
    return load_board_config()


def fold(
    projection: ModelLabSliProjection,
    evaluations: tuple[ModelLabSliEvaluated, ...],
    keep: int,
) -> ModelLabSliProjection:
    """Append one point per verdict, keep the newest ``keep`` per indicator."""
    seen = {(p.sli_id, p.interval_start) for p in projection.points}
    points = list(projection.points)
    for e in evaluations:
        if (e.sli_id, e.interval_start) in seen:
            continue
        points.append(
            ModelLabSliHistoryPoint(
                sli_id=e.sli_id,
                interval_start=e.interval_start,
                status=e.status,
                severity=e.severity,
                value=e.value,
                worst_subject=e.worst_subject,
            )
        )
    points.sort(key=lambda p: (p.sli_id, p.interval_start))
    kept: list[ModelLabSliHistoryPoint] = []
    by_id: dict[str, list[ModelLabSliHistoryPoint]] = {}
    for p in points:
        by_id.setdefault(p.sli_id, []).append(p)
    for series in by_id.values():
        kept.extend(series[-keep:])
    return ModelLabSliProjection(points=tuple(kept), latest=evaluations)


def trend(points: list[ModelLabSliHistoryPoint]) -> str:
    """A sparkline of the measured values and a status mark per point."""
    values = [p.value for p in points if p.value is not None]
    if not values:
        return "".join(_STATUS_MARK[p.status] for p in points)
    low, high = min(values), max(values)
    span = high - low
    out = []
    for p in points:
        if p.value is None:
            out.append(_STATUS_MARK[p.status])
        else:
            level = (
                0 if span == 0 else round((p.value - low) / span * (len(_SPARK) - 1))
            )
            out.append(_SPARK[level])
    return "".join(out)


def _value(e: ModelLabSliEvaluated) -> str:
    return "-" if e.value is None else f"{e.value:g} {e.unit}"


class HandlerLabSliBoard:
    """One page, every indicator, its severity and its trend."""

    def __init__(self, config: ModelLabSliBoardConfig | None = None) -> None:
        self._config = config if config is not None else _contract_config()

    def handle(self, request: ModelLabSliBoardRequest) -> ModelLabSliBoard:
        projection = fold(
            request.projection,
            request.evaluation.evaluations,
            self._config.history_points_per_indicator,
        )
        severity_rank = {s: i for i, s in enumerate(EnumLabSliSeverity)}
        rows = sorted(
            projection.latest,
            key=lambda e: (severity_rank[e.severity], _STATUS_ORDER[e.status]),
        )
        series: dict[str, list[ModelLabSliHistoryPoint]] = {}
        for p in projection.points:
            series.setdefault(p.sli_id, []).append(p)
        heading = (
            f"Lab SLI board, interval {request.evaluation.interval_start:%Y-%m-%dT%H:%MZ},"
            f" collector {request.evaluation.collector_host}"
        )
        breaches = sum(1 for e in rows if e.status is EnumLabSliStatus.BREACH)
        dark = sum(1 for e in rows if e.status is EnumLabSliStatus.UNKNOWN)
        summary = f"{breaches} breaching, {dark} dark, {len(rows)} indicators"

        md = [f"# {heading}", "", summary, ""]
        md.append(
            "| severity | indicator | status | value | target | worst | trend | action |"
        )
        md.append("| -- | -- | -- | -- | -- | -- | -- | -- |")
        cells = []
        for e in rows:
            spark = trend(series.get(e.sli_id, [])[-self._config.trend_points :])
            target = f"{e.comparator.value} {e.target:g}"
            md.append(
                f"| {e.severity.value} | {e.sli_id} | {e.status.value} | {_value(e)}"
                f" | {target} | {e.worst_subject or '-'} | {spark} | {e.action} |"
            )
            cells.append(
                "<tr class='{cls}'><td>{sev}</td><td title='{title}'>{sli}</td>"
                "<td>{status}</td><td>{value}</td><td>{target}</td><td>{worst}</td>"
                "<td class='trend'>{spark}</td><td>{action}</td></tr>".format(
                    cls=html.escape(e.status.value),
                    sev=e.severity.value,
                    title=html.escape(e.title, quote=True),
                    sli=html.escape(e.sli_id),
                    status=html.escape(e.status.value),
                    value=html.escape(_value(e)),
                    target=html.escape(target),
                    worst=html.escape(e.worst_subject or "-"),
                    spark=html.escape(spark),
                    action=html.escape(e.action),
                )
            )
        for problem in request.evaluation.source_errors + request.evaluation.rejected:
            md.append(f"\nsource: {problem}")
        page = (
            "<!doctype html><html><head><meta charset='utf-8'>"
            f"<title>{html.escape(heading)}</title><style>"
            "body{font-family:system-ui,sans-serif;margin:1.5rem}"
            "table{border-collapse:collapse}td,th{padding:.25rem .5rem;"
            "border-bottom:1px solid #ddd;text-align:left;vertical-align:top}"
            "tr.breach td:nth-child(3){color:#b00020;font-weight:600}"
            "tr.unknown td:nth-child(3){color:#8a6d00}"
            ".trend{font-family:ui-monospace,monospace}</style></head><body>"
            f"<h1>{html.escape(heading)}</h1><p>{html.escape(summary)}</p>"
            "<table><thead><tr><th>severity</th><th>indicator</th><th>status</th>"
            "<th>value</th><th>target</th><th>worst</th><th>trend</th><th>action</th>"
            "</tr></thead><tbody>"
            + "".join(cells)
            + "</tbody></table>"
            + "".join(
                f"<p class='source'>source: {html.escape(p)}</p>"
                for p in request.evaluation.source_errors + request.evaluation.rejected
            )
            + "</body></html>"
        )
        return ModelLabSliBoard(
            projection=projection, html=page, markdown="\n".join(md) + "\n"
        )


__all__ = ["HandlerLabSliBoard", "fold", "load_board_config", "trend"]
