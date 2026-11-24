# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""In-memory ports of node_build_delegation_checks: the recorded-results executor, a store, a merge source and a recording sink."""

from __future__ import annotations

from collections.abc import Mapping

from omnimarket.models.delegation_check_regression import ModelRegressionEvent
from omnimarket.models.lab_alarm_delivery import ModelLabAlarmDeliveryRequest
from omnimarket.models.test_selector_reading import ModelAttributionCandidate
from omnimarket.nodes.node_build_delegation_checks.models.model_build_delegation_checks import (
    EnumCheckReading,
    ModelBuildChecksResult,
    ModelBuildRecord,
    ModelCheckOutcome,
    ModelCheckRecord,
)


class RecordedResultsExecutor:
    """Answers each check from results recorded earlier, per build id then check id. A check with no recorded result for
    the build is unavailable: replay never invents a reading."""

    def __init__(self, recorded: Mapping[str, Mapping[str, ModelCheckOutcome]]) -> None:
        self._recorded = recorded
        self.calls: list[tuple[str, str]] = []

    def run(
        self, check_id: str, build_id: str, timeout_seconds: float
    ) -> ModelCheckOutcome:
        self.calls.append((check_id, build_id))
        outcome = self._recorded.get(build_id, {}).get(check_id)
        if outcome is None:
            return ModelCheckOutcome(
                reading=EnumCheckReading.UNAVAILABLE,
                detail="no recorded result for this check on this build",
            )
        return outcome


class InMemoryBuildStore:
    def __init__(self) -> None:
        self.builds: list[ModelBuildRecord] = []

    def begin(self, record: ModelBuildRecord) -> bool:
        if any(b.build_id == record.build_id for b in self.builds):
            return False
        self.builds.append(record)
        return True

    def previous_build(
        self, runtime_lane: str, build_id: str
    ) -> ModelBuildRecord | None:
        for build in reversed(self.builds):
            if build.runtime_lane == runtime_lane and build.build_id != build_id:
                return build
        return None

    def last_reading(
        self, check_id: str, before_build_id: str
    ) -> ModelCheckRecord | None:
        for build in reversed(self.builds):
            if build.build_id == before_build_id:
                continue
            for record in build.checks:
                if (
                    record.check_id == check_id
                    and record.reading is not EnumCheckReading.UNAVAILABLE
                ):
                    return record
        return None

    def finish(self, record: ModelBuildRecord) -> None:
        self.builds = [
            record if b.build_id == record.build_id else b for b in self.builds
        ]


class InMemoryMergeSource:
    """Merges keyed by (base, head); a pair not declared has no merges."""

    def __init__(
        self, ranges: Mapping[tuple[str, str], tuple[ModelAttributionCandidate, ...]]
    ) -> None:
        self._ranges = ranges

    def merges_between(
        self, base_sha: str, head_sha: str
    ) -> tuple[ModelAttributionCandidate, ...]:
        return self._ranges.get((base_sha, head_sha), ())


class RecordingSink:
    def __init__(self) -> None:
        self.regressions: list[ModelRegressionEvent] = []
        self.alarms: list[ModelLabAlarmDeliveryRequest] = []
        self.results: list[ModelBuildChecksResult] = []
        self.check_records: list[ModelCheckRecord] = []

    def publish_regression(self, event: ModelRegressionEvent) -> None:
        self.regressions.append(event)

    def request_alarm(self, request: ModelLabAlarmDeliveryRequest) -> None:
        self.alarms.append(request)

    def publish_result(self, result: ModelBuildChecksResult) -> None:
        self.results.append(result)

    def publish_check_record(self, record: ModelCheckRecord) -> None:
        self.check_records.append(record)


__all__ = [
    "InMemoryBuildStore",
    "InMemoryMergeSource",
    "RecordedResultsExecutor",
    "RecordingSink",
]
