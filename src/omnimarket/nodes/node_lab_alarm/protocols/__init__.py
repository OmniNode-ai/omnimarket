# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ports of node_lab_alarm: the manifest, the broker's lag and the previous sample."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Protocol

from omnimarket.models.lab_alarm_delivery import (
    ModelLabAlarmDeliveryRequest,
    ModelLabAlarmDeliveryResult,
)
from omnimarket.nodes.node_lab_alarm.models.model_lab_alarm_delegation_chain_canary import (
    ModelCanaryRun,
)
from omnimarket.nodes.node_lab_alarm.models.model_lab_alarm_effects_held import (
    ModelEffectsGroupReading,
    ModelEscalationBrief,
)


class ManifestUnreadableError(Exception):
    """The runtime's served manifest could not be read; the message names the cause."""


class ManifestUnreachableError(ManifestUnreadableError):
    """The manifest endpoint did not answer (refused, reset, timed out); no body was read.

    Distinct from a body that arrived and was refused: an unreachable endpoint says nothing about lag,
    and during a dev redeploy it is expected for the minutes the runtime takes to come up.
    """


class GroupNeverCommittedError(Exception):
    """The group holds no committed offset on any partition."""


class GroupUnreadableError(Exception):
    """The group's lag could not be read (refused, broker down); the message names the type."""


class RunRecordUnwritableError(Exception):
    """The run record could not be appended; the message names the type of the failure, never its text."""


class ProtocolManifestSource(Protocol):
    def read(self) -> Mapping[str, Any]:
        """The running image's auto-wiring manifest, or raise ManifestUnreadableError."""
        ...


class ProtocolManifestCache(Protocol):
    def load(self) -> tuple[Mapping[str, Any], datetime] | None:
        """The last good manifest (the contracts the declared nodes need) and when it was fetched, or None."""
        ...

    def save(self, manifest: Mapping[str, Any], fetched_at: datetime) -> None: ...


class ProtocolGroupLagReader(Protocol):
    def total_lag(self, group: str) -> int:
        """Committed offsets against end offsets, summed; or raise one of the two errors above."""
        ...


class ProtocolSampleStore(Protocol):
    def load(self) -> dict[str, int] | None:
        """The previous sample, or None when there is none."""
        ...

    def save(self, sample: Mapping[str, int]) -> None: ...


class ProtocolGrowthStreakStore(Protocol):
    def load(self) -> dict[str, int]:
        """Each group's count of consecutive growth samples, empty when there is none."""
        ...

    def save(self, streaks: Mapping[str, int]) -> None: ...


class ProtocolRunRecordStore(Protocol):
    def append(self, record: Mapping[str, Any]) -> None:
        """Add one finished run to the record the floor reads, or raise RunRecordUnwritableError."""


class EffectsGroupReadError(Exception):
    """The effects consumer groups could not be listed; the message names only the cause's type."""


class LaneDispatchError(Exception):
    """The lane that owns an alarm could not be started; the message names why."""


class ProtocolEffectsGroupReader(Protocol):
    def read(self) -> tuple[ModelEffectsGroupReading, ...]:
        """Every group matching the declared prefix and suffix, with its state and live member count, or raise EffectsGroupReadError."""
        ...


class ProtocolHeldStateStore(Protocol):
    def load(self) -> dict[str, str]:
        """The effects-held episode state (``held_since``, ``escalated_at``), empty when there is none."""
        ...

    def save(self, state: Mapping[str, str]) -> None: ...


class ProtocolLaneDispatcher(Protocol):
    def dispatch(self, brief: ModelEscalationBrief) -> None:
        """Start one lane for the brief, or raise LaneDispatchError."""
        ...


class ListenerUnresolvableError(Exception):
    """The address's name cannot be resolved: the alarm host's fault, not evidence about the broker."""


class ListenerRefusedError(Exception):
    """The address refused, timed out, closed before answering, or answered with a foreign correlation id."""


class ProtocolListenerProbe(Protocol):
    def probe(self, host: str, port: int, timeout: float) -> None:
        """Return when ``host:port`` answers a Kafka ApiVersions request, or raise one of the two errors above."""
        ...


class HeartbeatUnreadableError(Exception):
    """The deploy agent's heartbeat document could not be read: no answer, not HTTP 200, or not a JSON object."""


class ProtocolHeartbeatSource(Protocol):
    def read(self) -> Mapping[str, Any]:
        """The agent's heartbeat document; raises HeartbeatUnreadableError when it cannot be read."""
        ...


class CanaryRunsUnreadableError(Exception):
    """The canary workflow's runs could not be read: the gateway did not answer or answered something untyped."""


class ProtocolCanaryRunSource(Protocol):
    def read(self) -> tuple[ModelCanaryRun, ...]:
        """The newest runs of the canary workflow; raises CanaryRunsUnreadableError when they cannot be read."""
        ...


class LedgerUnreadableError(Exception):
    """The ledger file could not be read, or its tail holds no canonical row."""


class ProjectionUnreadableError(Exception):
    """The projection's newest row could not be read."""


class ProtocolLedgerNewestReader(Protocol):
    def newest(self) -> tuple[datetime, str]:
        """The timestamp and row type of the ledger's newest canonical row; raises LedgerUnreadableError."""
        ...


class ProtocolProjectionNewestReader(Protocol):
    def newest(self, since: datetime) -> datetime | None:
        """The newest row_ts at or after ``since``, None when there is none; raises ProjectionUnreadableError."""
        ...


class ProtocolAlarmDelivery(Protocol):
    def handle(
        self, request: ModelLabAlarmDeliveryRequest
    ) -> ModelLabAlarmDeliveryResult:
        """Deliver the alarms one condition read (node_lab_alarm_deliver_effect), or raise ValueError or OSError."""
        ...


__all__ = [
    "CanaryRunsUnreadableError",
    "EffectsGroupReadError",
    "GroupNeverCommittedError",
    "GroupUnreadableError",
    "HeartbeatUnreadableError",
    "LaneDispatchError",
    "LedgerUnreadableError",
    "ListenerRefusedError",
    "ListenerUnresolvableError",
    "ManifestUnreachableError",
    "ManifestUnreadableError",
    "ProjectionUnreadableError",
    "ProtocolAlarmDelivery",
    "ProtocolCanaryRunSource",
    "ProtocolEffectsGroupReader",
    "ProtocolGroupLagReader",
    "ProtocolGrowthStreakStore",
    "ProtocolHeartbeatSource",
    "ProtocolHeldStateStore",
    "ProtocolLaneDispatcher",
    "ProtocolLedgerNewestReader",
    "ProtocolListenerProbe",
    "ProtocolManifestCache",
    "ProtocolManifestSource",
    "ProtocolProjectionNewestReader",
    "ProtocolRunRecordStore",
    "ProtocolSampleStore",
    "RunRecordUnwritableError",
]
