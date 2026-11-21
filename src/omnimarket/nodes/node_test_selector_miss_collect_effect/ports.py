# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ports of the nightly miss collector: the repository gateway reads, the keyed alarm store and the alarm receiver."""

from __future__ import annotations

from datetime import date
from typing import Literal, Protocol

from omnimarket.models.lab_alarm_delivery import (
    ModelLabAlarmDeliveryRequest,
    ModelLabAlarmDeliveryResult,
)
from omnimarket.models.model_github_gateway_io import (
    GithubGatewayResult,
    ModelGithubGatewayRequest,
)
from omnimarket.models.test_selector_reading import ModelAttributionCandidate
from omnimarket.nodes.node_test_selector_miss_collect_effect.models.model_miss_collect import (
    ModelNightlyAlarm,
    ModelNightlyRun,
    ModelNightReading,
    ModelPreviousNight,
    ModelRangeCommit,
)


class GatewayReadError(Exception):
    """A gateway read failed; the night is then neither clean nor attributed."""


class ProtocolNightlyGateway(Protocol):
    """Reads through the repository gateway verbs (``onex scm run-list``, ``run-download``, ``run-view``)."""

    def nightly_run(self, repo: str, slot_date: date) -> ModelNightlyRun | None: ...

    def read_night(self, repo: str, run: ModelNightlyRun) -> ModelNightReading: ...

    def previous_night(
        self, repo: str, slot_date: date
    ) -> ModelPreviousNight | None: ...

    def candidates(
        self, repo: str, previous_head: str, head: str, retention_days: int
    ) -> tuple[ModelAttributionCandidate, ...]: ...


class ProtocolGatewayRead(Protocol):
    """One typed read through the repository gateway (in process or over the bus); it raises the gateway client's error."""

    def read(self, request: ModelGithubGatewayRequest) -> GithubGatewayResult: ...


class ProtocolCommitRange(Protocol):
    """The first-parent commits in ``previous_head..head``, oldest first. Read-only."""

    def first_parent(
        self, previous_head: str, head: str
    ) -> tuple[ModelRangeCommit, ...]: ...


class ProtocolAlarmStore(Protocol):
    """The alarm keyed upsert. The status moves ``pending`` to ``delivered``."""

    def upsert_pending(self, alarm: ModelNightlyAlarm) -> bool:
        """Create the alarm under its key when absent. Returns True only for the call that created it (atomic)."""
        ...

    def undelivered(self) -> tuple[ModelNightlyAlarm, ...]: ...

    def mark_delivered(self, key: str) -> None: ...

    def record_collector_result(self, repo: str, slot_date: date) -> None: ...

    def has_collector_result(self, repo: str, slot_date: date) -> bool: ...


class ProtocolAlarmReceiver(Protocol):
    """The alarm routing's receiver. Idempotent by the alarm key: a repeat is ``duplicate``."""

    def accept(self, alarm: ModelNightlyAlarm) -> Literal["accepted", "duplicate"]: ...


class ProtocolAlarmRoutingDelivery(Protocol):
    """The alarm routing's delivery (the shared request and result types of the lab alarm delivery)."""

    def handle(
        self, request: ModelLabAlarmDeliveryRequest
    ) -> ModelLabAlarmDeliveryResult: ...


__all__: list[str] = [
    "GatewayReadError",
    "ProtocolAlarmReceiver",
    "ProtocolAlarmRoutingDelivery",
    "ProtocolAlarmStore",
    "ProtocolCommitRange",
    "ProtocolGatewayRead",
    "ProtocolNightlyGateway",
]
