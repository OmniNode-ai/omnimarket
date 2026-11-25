# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The day-over-day change of the census daily record: ``handle(ModelCensusDailyEventRequest) -> ModelCensusDailyEvent``.

Pure: no clock, no file, no bus. The change is read over the repositories counted on both days (a row whose state is
``absent`` or ``unreadable`` on either day, or a repository on one day only, is skipped and named). A previous record dated
the same day or later is not a previous day. With no previous day the event says so and carries no change.
"""

from __future__ import annotations

from omnimarket.nodes.node_work_ledger_file_census_effect.models.model_daily_change import (
    EnumCensusChangeBasis,
    ModelCensusDailyChange,
    ModelCensusDailyEvent,
    ModelCensusDailyEventRequest,
    ModelCensusRepositoryChange,
)
from omnimarket.nodes.node_work_ledger_file_census_effect.models.model_file_census import (
    ModelCensusDailyRow,
)

NOT_COUNTED = ("absent", "unreadable")


def _counted(row: ModelCensusDailyRow | None) -> bool:
    return row is not None and row.state not in NOT_COUNTED


class HandlerDailyChange:
    def handle(self, request: ModelCensusDailyEventRequest) -> ModelCensusDailyEvent:
        record = request.record
        absent = tuple(
            r.repository for r in record.repositories if r.state in NOT_COUNTED
        )
        return ModelCensusDailyEvent(
            host=request.host,
            record=record,
            partial=bool(absent),
            absent_repositories=absent,
            change=self._change(request),
        )

    def _change(self, request: ModelCensusDailyEventRequest) -> ModelCensusDailyChange:
        previous = request.previous
        if request.previous_unreadable:
            return ModelCensusDailyChange(
                basis=EnumCensusChangeBasis.HISTORY_UNREADABLE
            )
        if previous is None or previous.recorded_on >= request.record.recorded_on:
            return ModelCensusDailyChange(
                basis=EnumCensusChangeBasis.NO_PREVIOUS_RECORD
            )
        before = {r.repository: r for r in previous.repositories}
        compared: list[ModelCensusRepositoryChange] = []
        skipped: list[str] = []
        for row in request.record.repositories:
            old = before.get(row.repository)
            if not (_counted(row) and _counted(old)):
                skipped.append(row.repository)
                continue
            assert old is not None
            compared.append(
                ModelCensusRepositoryChange(
                    repository=row.repository,
                    files_before=old.files,
                    files_after=row.files,
                    files_change=row.files - old.files,
                    runtime_files_before=old.runtime_files,
                    runtime_files_after=row.runtime_files,
                    runtime_files_change=row.runtime_files - old.runtime_files,
                )
            )
        own_line = {r.repository for r in request.record.repositories if r.own_line}
        in_total = [c for c in compared if c.repository not in own_line]
        return ModelCensusDailyChange(
            basis=EnumCensusChangeBasis.PREVIOUS_DAY,
            previous_recorded_on=previous.recorded_on,
            compared_repositories=tuple(c.repository for c in compared),
            skipped_repositories=tuple(skipped),
            total_files_change=sum(c.files_change for c in in_total),
            runtime_files_change=sum(c.runtime_files_change for c in in_total),
            repositories=tuple(compared),
        )
