"""HandlerProcessWatchdog — infrastructure process watchdog compute node.

Checks health of critical platform components (emit daemon, Kafka consumers,
LLM endpoints, Docker containers) and produces structured health reports.
Uses CheckTarget protocol for injectable check targets (mock in tests, real
HTTP/socket/Docker targets in production).
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from omnibase_compat.protocols.protocol_health_check import (
    ProtocolHealthCheck as CheckTarget,
)

from omnimarket.nodes.node_process_watchdog.models.model_watchdog_completed_event import (
    ModelWatchdogCompletedEvent,
)
from omnimarket.nodes.node_process_watchdog.models.model_watchdog_start_command import (
    ModelWatchdogStartCommand,
)
from omnimarket.nodes.node_process_watchdog.models.model_watchdog_state import (
    EnumCheckStatus,
    EnumCheckTarget,
    ModelWatchdogCheckResult,
    ModelWatchdogReport,
)

logger = logging.getLogger(__name__)

# Severity ordering for aggregation: worst status wins
_STATUS_SEVERITY: dict[EnumCheckStatus, int] = {
    EnumCheckStatus.HEALTHY: 0,
    EnumCheckStatus.UNKNOWN: 1,
    EnumCheckStatus.DEGRADED: 2,
    EnumCheckStatus.DOWN: 3,
}


class InmemoryCheckTarget:
    """Mock check target for testing. Returns preconfigured results."""

    def __init__(
        self,
        name: str,
        category: EnumCheckTarget,
        status: EnumCheckStatus = EnumCheckStatus.HEALTHY,
        message: str = "",
        details: dict[str, object] | None = None,
        restart_result: bool = True,
    ) -> None:
        self._name = name
        self._category = category
        self._status = status
        self._message = message
        self._details = details or {}
        self._restart_result = restart_result
        self.restart_called = False

    @property
    def name(self) -> str:
        return self._name

    @property
    def category(self) -> EnumCheckTarget:
        return self._category

    def check(self) -> ModelWatchdogCheckResult:
        return ModelWatchdogCheckResult(
            target=self._name,
            category=self._category,
            status=self._status,
            message=self._message,
            details=self._details,
        )

    def restart(self) -> bool:
        self.restart_called = True
        return self._restart_result


def _worst_status(statuses: list[EnumCheckStatus]) -> EnumCheckStatus:
    """Return the worst (highest severity) status from a list."""
    if not statuses:
        return EnumCheckStatus.UNKNOWN
    return max(statuses, key=lambda s: _STATUS_SEVERITY[s])


class HandlerProcessWatchdog:
    """Handler for infrastructure process watchdog.

    Pure logic with injectable check targets for testability.
    """

    def run_checks(
        self,
        command: ModelWatchdogStartCommand,
        targets: list[CheckTarget],
    ) -> ModelWatchdogReport:
        """Execute all check targets and aggregate results into a report."""
        results: list[ModelWatchdogCheckResult] = []
        alerts_emitted = 0
        restarts_attempted = 0

        # Filter targets to only those requested in the command
        requested_categories = set(command.check_targets)

        for target in targets:
            if target.category not in requested_categories:
                continue

            result = target.check()

            # Auto-restart on DOWN if not dry_run
            if result.status == EnumCheckStatus.DOWN and not command.dry_run:
                restart_ok = target.restart()
                restarts_attempted += 1
                result = ModelWatchdogCheckResult(
                    target=result.target,
                    category=result.category,
                    status=result.status,
                    message=result.message,
                    details=result.details,
                    restart_attempted=True,
                    restart_succeeded=restart_ok,
                )

            # Count alerts for DOWN and optionally DEGRADED
            if result.status == EnumCheckStatus.DOWN or (
                result.status == EnumCheckStatus.DEGRADED and command.alert_on_degraded
            ):
                alerts_emitted += 1

            results.append(result)

        # Aggregate counts
        statuses = [r.status for r in results]
        healthy_count = sum(1 for s in statuses if s == EnumCheckStatus.HEALTHY)
        degraded_count = sum(1 for s in statuses if s == EnumCheckStatus.DEGRADED)
        down_count = sum(1 for s in statuses if s == EnumCheckStatus.DOWN)
        unknown_count = sum(1 for s in statuses if s == EnumCheckStatus.UNKNOWN)

        overall_status = _worst_status(statuses)

        return ModelWatchdogReport(
            overall_status=overall_status,
            checks=results,
            total_checks=len(results),
            healthy_count=healthy_count,
            degraded_count=degraded_count,
            down_count=down_count,
            unknown_count=unknown_count,
            alerts_emitted=alerts_emitted,
            restarts_attempted=restarts_attempted,
            correlation_id=command.correlation_id,
            dry_run=command.dry_run,
        )

    def make_completed_event(
        self,
        report: ModelWatchdogReport,
        started_at: datetime,
    ) -> ModelWatchdogCompletedEvent:
        """Create a completion event from the watchdog report."""
        return ModelWatchdogCompletedEvent(
            correlation_id=report.correlation_id,
            overall_status=report.overall_status,
            started_at=started_at,
            completed_at=datetime.now(tz=UTC),
            report=report,
        )

    def serialize_completed(self, event: ModelWatchdogCompletedEvent) -> bytes:
        """Serialize a completed event to bytes."""
        return json.dumps(event.model_dump(mode="json")).encode()

    def handle(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """RuntimeLocal handler protocol shim.

        Delegates to run_watchdog with a ModelWatchdogStartCommand and
        InmemoryCheckTarget instances constructed from input_data.
        """
        targets_data = input_data.pop("targets", [])
        command = ModelWatchdogStartCommand(**input_data)
        targets: list[CheckTarget] = [
            InmemoryCheckTarget(
                name=t["name"],
                category=EnumCheckTarget(t["category"]),
                status=EnumCheckStatus(t.get("status", "healthy")),
                message=t.get("message", ""),
            )
            for t in targets_data
        ]
        report, _completed = self.run_watchdog(command, targets)
        return report.model_dump(mode="json")

    def run_watchdog(
        self,
        command: ModelWatchdogStartCommand,
        targets: list[CheckTarget],
    ) -> tuple[ModelWatchdogReport, ModelWatchdogCompletedEvent]:
        """Run a complete watchdog cycle.

        Deterministic entry point for testing.
        """
        started_at = datetime.now(tz=UTC)
        report = self.run_checks(command, targets)
        completed = self.make_completed_event(report, started_at)
        return report, completed


__all__: list[str] = [
    "CheckTarget",
    "HandlerProcessWatchdog",
    "InmemoryCheckTarget",
]
