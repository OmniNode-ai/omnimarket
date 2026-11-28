# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ports of node_build_delegation_checks: the probe runner, the build store, the merge source and the event sink."""

from __future__ import annotations

from typing import Protocol

from omnimarket.models.delegation_check_regression import ModelRegressionEvent
from omnimarket.models.lab_alarm_delivery import ModelLabAlarmDeliveryRequest
from omnimarket.models.test_selector_reading import ModelAttributionCandidate
from omnimarket.nodes.node_build_delegation_checks.models.model_build_delegation_checks import (
    ModelBuildChecksResult,
    ModelBuildRecord,
    ModelCheckOutcome,
    ModelCheckRecord,
)


class BuildStoreUnwritableError(OSError):
    """The build store cannot write its history where it was told to: the refusal names the path and the cause."""


class UnresolvableRangeError(OSError):
    """git cannot resolve a build range (rc=128, for example a build sha absent from the clone): the merges are unknown."""


class ProtocolCheckRunner(Protocol):
    """Runs one check's probe against one build. A probe that cannot run answers UNAVAILABLE, never FAIL."""

    def run(
        self, check_id: str, build_id: str, timeout_seconds: float
    ) -> ModelCheckOutcome: ...


class ProtocolBuildStore(Protocol):
    def begin(self, record: ModelBuildRecord) -> bool:
        """Claim the build id; False when a record for it already exists (a replay)."""
        ...

    def previous_build(
        self, runtime_lane: str, build_id: str
    ) -> ModelBuildRecord | None:
        """The newest recorded build of the lane other than ``build_id``."""
        ...

    def last_reading(
        self, check_id: str, before_build_id: str
    ) -> ModelCheckRecord | None:
        """The newest pass or fail record of the check on a build other than ``before_build_id``."""
        ...

    def finish(self, record: ModelBuildRecord) -> None: ...


class ProtocolMergeSource(Protocol):
    def merges_between(
        self, base_sha: str, head_sha: str
    ) -> tuple[ModelAttributionCandidate, ...]:
        """The first-parent commits on the branch after ``base_sha`` up to ``head_sha``, oldest first."""
        ...


class ProtocolRegressionSink(Protocol):
    def publish_regression(self, event: ModelRegressionEvent) -> None: ...

    def request_alarm(self, request: ModelLabAlarmDeliveryRequest) -> None: ...

    def publish_result(self, result: ModelBuildChecksResult) -> None: ...

    def publish_check_record(self, record: ModelCheckRecord) -> None: ...


__all__ = [
    "BuildStoreUnwritableError",
    "ProtocolBuildStore",
    "ProtocolCheckRunner",
    "ProtocolMergeSource",
    "ProtocolRegressionSink",
    "UnresolvableRangeError",
]
