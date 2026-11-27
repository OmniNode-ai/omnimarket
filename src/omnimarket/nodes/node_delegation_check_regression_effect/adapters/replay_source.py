# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A check source over recorded readings: the replay of a recorded transition through the run.

``previous`` answers with the newest recorded reading at or before ``previous_through``; ``run`` answers with the newest
recorded reading after ``previous_through`` and at or before ``current_through``. A check with no such recorded reading
answers None: the replay never invents one.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime

from omnimarket.nodes.node_delegation_check_regression_effect.models.model_check_regression import (
    ModelCheckReading,
)


def _stamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class ReplayCheckSource:
    def __init__(
        self,
        readings: Iterable[ModelCheckReading],
        *,
        previous_through: datetime,
        current_through: datetime,
    ) -> None:
        self._readings = sorted(readings, key=lambda r: r.read_at)
        self._previous_through = previous_through
        self._current_through = current_through
        #: Every check the run asked this source to run, in order.
        self.runs: list[str] = []

    @classmethod
    def from_readings(
        cls,
        rows: Iterable[Mapping[str, str]],
        *,
        previous_through: str,
        current_through: str,
    ) -> ReplayCheckSource:
        return cls(
            (
                ModelCheckReading.model_validate(
                    {**row, "read_at": _stamp(row["read_at"])}
                )
                for row in rows
            ),
            previous_through=_stamp(previous_through),
            current_through=_stamp(current_through),
        )

    def previous(self, check_id: str, *, before: datetime) -> ModelCheckReading | None:
        found = [
            r
            for r in self._readings
            if r.check_id == check_id and r.read_at <= self._previous_through
        ]
        return found[-1] if found else None

    def run(
        self, check_id: str, *, since: datetime, timeout_seconds: int
    ) -> ModelCheckReading | None:
        self.runs.append(check_id)
        found = [
            r
            for r in self._readings
            if r.check_id == check_id
            and self._previous_through < r.read_at <= self._current_through
        ]
        return found[-1] if found else None


__all__ = ["ReplayCheckSource"]
