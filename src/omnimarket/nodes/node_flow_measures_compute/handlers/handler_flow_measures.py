# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Compute the flow measures from supplied facts and ``now`` (OMN-21441).

Canonical definition-B shape: ``handle(ModelFlowMeasuresRequest) ->
ModelFlowMeasuresResult``. Pure: it reads no clock, file, network or protocol. The
trailing window is ``config.wip_gate.trailing_days`` of this node's contract,
read through ``contract_section``.
"""

from __future__ import annotations

import statistics
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from omnimarket.nodes.contract_topics import contract_section
from omnimarket.nodes.node_flow_measures_compute.models.model_flow_measures import (
    EnumFillEvent,
    EnumFlowMeasure,
    EnumHoldReason,
    ModelFlowMeasure,
    ModelFlowMeasuresConfig,
    ModelFlowMeasuresRequest,
    ModelFlowMeasuresResult,
    ModelStopRecord,
)

CONTRACT_PATH = Path(__file__).resolve().parent.parent / "contract.yaml"
_SECONDS_PER_HOUR = 3600.0
_UNREAD = "unread"
_ALL = "all"


def load_flow_measures_config(path: Path = CONTRACT_PATH) -> ModelFlowMeasuresConfig:
    """Read ``config.wip_gate`` of a contract; fail closed when it is absent."""
    return ModelFlowMeasuresConfig.model_validate(
        contract_section(path, "config", "wip_gate")
    )


def _stamp(at: datetime | None) -> str:
    return "never" if at is None else at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _minutes(seconds: float) -> str:
    return f"{seconds / 60:.1f} min"


def _measure(
    measure: EnumFlowMeasure,
    subject: str,
    label: str,
    value: float | None,
    display: str,
    read_at: datetime | None,
    basis: str,
) -> ModelFlowMeasure:
    shown = _UNREAD if value is None else display
    return ModelFlowMeasure(
        measure=measure,
        subject=subject,
        label=label,
        value=value,
        display=shown,
        read_at=read_at,
        basis=basis,
        line=f"{label} {subject}: {shown} | read {_stamp(read_at)} | basis {basis}",
    )


class HandlerFlowMeasures:
    """Merges per hour, green-to-merged median, held PRs, stop times, fill counts."""

    def __init__(self, config: ModelFlowMeasuresConfig | None = None) -> None:
        self._config = config if config is not None else load_flow_measures_config()

    def handle(self, request: ModelFlowMeasuresRequest) -> ModelFlowMeasuresResult:
        days = self._config.trailing_days
        now = request.now
        start = now - timedelta(days=days)
        window = f"trailing {days}d to {_stamp(now)}"

        def inside(at: datetime) -> bool:
            return start < at <= now

        repositories = self._repositories(request)
        measures: list[ModelFlowMeasure] = []
        measures.extend(self._merge_rates(request, repositories, window, inside, days))
        measures.extend(self._green_to_merged(request, repositories, window, inside))
        measures.extend(self._held(request, repositories))
        measures.extend(self._stop_times(request, window, inside))
        measures.extend(self._fill_counts(request, repositories, window, inside))
        return ModelFlowMeasuresResult(
            now=now,
            trailing_days=days,
            measures=tuple(measures),
            lines=tuple(m.line for m in measures),
        )

    @staticmethod
    def _repositories(request: ModelFlowMeasuresRequest) -> tuple[str, ...]:
        names = set(request.repositories)
        for source in (request.merges, request.held, request.fill):
            if source is not None:
                names.update(source.repositories_read)
        return tuple(sorted(names)) or (_ALL,)

    @staticmethod
    def _merge_rates(
        request: ModelFlowMeasuresRequest,
        repositories: tuple[str, ...],
        window: str,
        inside: Callable[[datetime], bool],
        days: int,
    ) -> Iterable[ModelFlowMeasure]:
        source = request.merges
        for repo in repositories:
            if source is None or repo not in source.repositories_read:
                yield _measure(
                    EnumFlowMeasure.MERGES_PER_HOUR,
                    repo,
                    "merges_per_hour",
                    None,
                    "",
                    None,
                    "repository not read"
                    if source is not None
                    else "no merge source read",
                )
                continue
            merged = sum(
                1 for m in source.merges if m.repository == repo and inside(m.merged_at)
            )
            rate = merged / (days * 24)
            yield _measure(
                EnumFlowMeasure.MERGES_PER_HOUR,
                repo,
                "merges_per_hour",
                rate,
                f"{rate:.3f}/h",
                source.read_at,
                f"{source.basis}; {merged} merges, {window}",
            )

    @staticmethod
    def _green_to_merged(
        request: ModelFlowMeasuresRequest,
        repositories: tuple[str, ...],
        window: str,
        inside: Callable[[datetime], bool],
    ) -> Iterable[ModelFlowMeasure]:
        source = request.merges
        label = "green_to_merged_median"
        for repo in repositories:
            if source is None or repo not in source.repositories_read:
                yield _measure(
                    EnumFlowMeasure.GREEN_TO_MERGED_MEDIAN,
                    repo,
                    label,
                    None,
                    "",
                    None,
                    "repository not read"
                    if source is not None
                    else "no merge source read",
                )
                continue
            spans = [
                (m.merged_at - m.green_at).total_seconds()
                for m in source.merges
                if m.repository == repo
                and inside(m.merged_at)
                and m.green_at is not None
                and m.green_at <= m.merged_at
            ]
            if not spans:
                yield _measure(
                    EnumFlowMeasure.GREEN_TO_MERGED_MEDIAN,
                    repo,
                    label,
                    None,
                    "",
                    source.read_at,
                    f"{source.basis}; no merge with a green time, {window}",
                )
                continue
            median = statistics.median(spans)
            yield _measure(
                EnumFlowMeasure.GREEN_TO_MERGED_MEDIAN,
                repo,
                label,
                median,
                _minutes(median),
                source.read_at,
                f"{source.basis}; median of {len(spans)} merges, {window}",
            )

    @staticmethod
    def _held(
        request: ModelFlowMeasuresRequest, repositories: tuple[str, ...]
    ) -> Iterable[ModelFlowMeasure]:
        source = request.held
        for repo in repositories:
            for reason in EnumHoldReason:
                if source is None or repo not in source.repositories_read:
                    yield _measure(
                        EnumFlowMeasure.PRS_HELD,
                        repo,
                        reason.value,
                        None,
                        "",
                        None,
                        "repository not read"
                        if source is not None
                        else "no held-PR source read",
                    )
                    continue
                count = sum(
                    1
                    for h in source.held
                    if h.repository == repo and h.reason == reason
                )
                yield _measure(
                    EnumFlowMeasure.PRS_HELD,
                    repo,
                    reason.value,
                    float(count),
                    str(count),
                    source.read_at,
                    f"{source.basis}; open PRs at read",
                )

    @staticmethod
    def _stop_times(
        request: ModelFlowMeasuresRequest,
        window: str,
        inside: Callable[[datetime], bool],
    ) -> Iterable[ModelFlowMeasure]:
        source = request.stops
        pairs: tuple[tuple[str, Callable[[ModelStopRecord], float | None]], ...] = (
            ("detection_to_stop", _detection_to_stop),
            ("stop_to_fix", _stop_to_fix),
        )
        for label, span in pairs:
            if source is None:
                yield _measure(
                    EnumFlowMeasure.STOP_TIMES,
                    _ALL,
                    label,
                    None,
                    "",
                    None,
                    "no stop source read",
                )
                continue
            spans = [
                s
                for r in source.records
                if inside(r.detected_at) and (s := span(r)) is not None
            ]
            if not spans:
                yield _measure(
                    EnumFlowMeasure.STOP_TIMES,
                    _ALL,
                    label,
                    None,
                    "",
                    source.read_at,
                    f"{source.basis}; no stop record with this span, {window}",
                )
                continue
            median = statistics.median(spans)
            yield _measure(
                EnumFlowMeasure.STOP_TIMES,
                _ALL,
                label,
                median,
                _minutes(median),
                source.read_at,
                f"{source.basis}; median of {len(spans)} stops, {window}",
            )

    @staticmethod
    def _fill_counts(
        request: ModelFlowMeasuresRequest,
        repositories: tuple[str, ...],
        window: str,
        inside: Callable[[datetime], bool],
    ) -> Iterable[ModelFlowMeasure]:
        source = request.fill
        for repo in repositories:
            for event in EnumFillEvent:
                if source is None or repo not in source.repositories_read:
                    yield _measure(
                        EnumFlowMeasure.FILL_COUNTS,
                        repo,
                        event.value,
                        None,
                        "",
                        None,
                        "repository not read"
                        if source is not None
                        else "no fill source read",
                    )
                    continue
                count = sum(
                    1
                    for e in source.events
                    if e.repository == repo and e.event == event and inside(e.at)
                )
                yield _measure(
                    EnumFlowMeasure.FILL_COUNTS,
                    repo,
                    event.value,
                    float(count),
                    str(count),
                    source.read_at,
                    f"{source.basis}; {window}",
                )


def _detection_to_stop(record: ModelStopRecord) -> float | None:
    if record.stopped_at is None or record.stopped_at < record.detected_at:
        return None
    return (record.stopped_at - record.detected_at).total_seconds()


def _stop_to_fix(record: ModelStopRecord) -> float | None:
    if record.stopped_at is None or record.fixed_at is None:
        return None
    if record.fixed_at < record.stopped_at:
        return None
    return (record.fixed_at - record.stopped_at).total_seconds()
