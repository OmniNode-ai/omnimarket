"""Golden chain tests for node_process_watchdog.

Verifies the compute node: start command -> health checks -> completion event,
various check outcomes, alerting logic, auto-restart, and EventBusInmemory wiring.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_process_watchdog.handlers.handler_process_watchdog import (
    HandlerProcessWatchdog,
    InmemoryCheckTarget,
)
from omnimarket.nodes.node_process_watchdog.models.model_watchdog_start_command import (
    ModelWatchdogStartCommand,
)
from omnimarket.nodes.node_process_watchdog.models.model_watchdog_state import (
    EnumCheckStatus,
    EnumCheckTarget,
)

CMD_TOPIC = "onex.cmd.omnimarket.watchdog-start.v1"
COMPLETED_TOPIC = "onex.evt.omnimarket.watchdog-completed.v1"


def _make_command(
    check_targets: list[EnumCheckTarget] | None = None,
    dry_run: bool = False,
    alert_on_degraded: bool = True,
) -> ModelWatchdogStartCommand:
    return ModelWatchdogStartCommand(
        check_targets=check_targets or list(EnumCheckTarget),
        correlation_id=str(uuid4()),
        dry_run=dry_run,
        alert_on_degraded=alert_on_degraded,
        requested_at=datetime.now(tz=UTC),
    )


def _healthy_targets() -> list[InmemoryCheckTarget]:
    """All four target categories, all healthy."""
    return [
        InmemoryCheckTarget(
            name="emit_daemon_socket",
            category=EnumCheckTarget.EMIT_DAEMON,
            status=EnumCheckStatus.HEALTHY,
            message="Socket responding",
        ),
        InmemoryCheckTarget(
            name="kafka_consumer_group_omnimarket",
            category=EnumCheckTarget.KAFKA_CONSUMERS,
            status=EnumCheckStatus.HEALTHY,
            message="1 member, 0 lag",
        ),
        InmemoryCheckTarget(
            name="llm_endpoint_8000",
            category=EnumCheckTarget.LLM_ENDPOINTS,
            status=EnumCheckStatus.HEALTHY,
            message="200 OK",
            details={"latency_ms": 42},
        ),
        InmemoryCheckTarget(
            name="docker_omnibase_infra_postgres",
            category=EnumCheckTarget.DOCKER_CONTAINERS,
            status=EnumCheckStatus.HEALTHY,
            message="running (healthy)",
        ),
    ]


@pytest.mark.unit
class TestProcessWatchdogGoldenChain:
    """Golden chain: start command -> health checks -> completion."""

    async def test_all_healthy(self, event_bus: EventBusInmemory) -> None:
        """All targets healthy -> overall HEALTHY, zero alerts."""
        handler = HandlerProcessWatchdog()
        targets = _healthy_targets()
        command = _make_command()

        report, completed = handler.run_watchdog(command, targets)

        assert report.overall_status == EnumCheckStatus.HEALTHY
        assert report.total_checks == 4
        assert report.healthy_count == 4
        assert report.degraded_count == 0
        assert report.down_count == 0
        assert report.alerts_emitted == 0
        assert report.restarts_attempted == 0
        assert completed.overall_status == EnumCheckStatus.HEALTHY

    async def test_one_target_down(self, event_bus: EventBusInmemory) -> None:
        """One target DOWN -> overall DOWN, alert emitted, restart attempted."""
        handler = HandlerProcessWatchdog()
        targets = [
            InmemoryCheckTarget(
                name="emit_daemon_socket",
                category=EnumCheckTarget.EMIT_DAEMON,
                status=EnumCheckStatus.DOWN,
                message="Socket not found",
                restart_result=True,
            ),
            InmemoryCheckTarget(
                name="llm_endpoint_8000",
                category=EnumCheckTarget.LLM_ENDPOINTS,
                status=EnumCheckStatus.HEALTHY,
                message="200 OK",
            ),
        ]
        command = _make_command()

        report, _completed = handler.run_watchdog(command, targets)

        assert report.overall_status == EnumCheckStatus.DOWN
        assert report.down_count == 1
        assert report.healthy_count == 1
        assert report.alerts_emitted == 1
        assert report.restarts_attempted == 1
        # Verify restart was called on the down target
        assert targets[0].restart_called is True
        assert targets[1].restart_called is False
        # Verify the check result has restart info
        down_check = report.checks[0]
        assert down_check.restart_attempted is True
        assert down_check.restart_succeeded is True

    async def test_degraded_target_alerts(self, event_bus: EventBusInmemory) -> None:
        """Degraded target with alert_on_degraded=True -> alert emitted."""
        handler = HandlerProcessWatchdog()
        targets = [
            InmemoryCheckTarget(
                name="kafka_consumer_group_events",
                category=EnumCheckTarget.KAFKA_CONSUMERS,
                status=EnumCheckStatus.DEGRADED,
                message="High lag: 1500 messages",
                details={"lag": 1500},
            ),
        ]
        command = _make_command(alert_on_degraded=True)

        report, _completed = handler.run_watchdog(command, targets)

        assert report.overall_status == EnumCheckStatus.DEGRADED
        assert report.degraded_count == 1
        assert report.alerts_emitted == 1
        assert report.restarts_attempted == 0

    async def test_degraded_no_alert_when_disabled(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Degraded target with alert_on_degraded=False -> no alert."""
        handler = HandlerProcessWatchdog()
        targets = [
            InmemoryCheckTarget(
                name="kafka_consumer_group_events",
                category=EnumCheckTarget.KAFKA_CONSUMERS,
                status=EnumCheckStatus.DEGRADED,
                message="High lag",
            ),
        ]
        command = _make_command(alert_on_degraded=False)

        report, _completed = handler.run_watchdog(command, targets)

        assert report.overall_status == EnumCheckStatus.DEGRADED
        assert report.alerts_emitted == 0

    async def test_dry_run_skips_restart(self, event_bus: EventBusInmemory) -> None:
        """dry_run=True -> DOWN target detected but no restart attempted."""
        handler = HandlerProcessWatchdog()
        targets = [
            InmemoryCheckTarget(
                name="emit_daemon_socket",
                category=EnumCheckTarget.EMIT_DAEMON,
                status=EnumCheckStatus.DOWN,
                message="Socket not found",
            ),
        ]
        command = _make_command(dry_run=True)

        report, _completed = handler.run_watchdog(command, targets)

        assert report.overall_status == EnumCheckStatus.DOWN
        assert report.down_count == 1
        assert report.restarts_attempted == 0
        assert report.dry_run is True
        assert targets[0].restart_called is False
        # Alert still counted (alerting is informational)
        assert report.alerts_emitted == 1

    async def test_filter_by_category(self, event_bus: EventBusInmemory) -> None:
        """Only requested check_targets are executed."""
        handler = HandlerProcessWatchdog()
        targets = _healthy_targets()  # All 4 categories
        command = _make_command(
            check_targets=[EnumCheckTarget.EMIT_DAEMON, EnumCheckTarget.LLM_ENDPOINTS]
        )

        report, _completed = handler.run_watchdog(command, targets)

        assert report.total_checks == 2
        categories = {c.category for c in report.checks}
        assert categories == {
            EnumCheckTarget.EMIT_DAEMON,
            EnumCheckTarget.LLM_ENDPOINTS,
        }

    async def test_multiple_down_targets(self, event_bus: EventBusInmemory) -> None:
        """Multiple DOWN targets -> multiple alerts and restarts."""
        handler = HandlerProcessWatchdog()
        targets = [
            InmemoryCheckTarget(
                name="emit_daemon_socket",
                category=EnumCheckTarget.EMIT_DAEMON,
                status=EnumCheckStatus.DOWN,
                message="Socket not found",
                restart_result=True,
            ),
            InmemoryCheckTarget(
                name="llm_endpoint_8001",
                category=EnumCheckTarget.LLM_ENDPOINTS,
                status=EnumCheckStatus.DOWN,
                message="Connection refused",
                restart_result=False,
            ),
            InmemoryCheckTarget(
                name="docker_postgres",
                category=EnumCheckTarget.DOCKER_CONTAINERS,
                status=EnumCheckStatus.HEALTHY,
                message="running",
            ),
        ]
        command = _make_command()

        report, _completed = handler.run_watchdog(command, targets)

        assert report.overall_status == EnumCheckStatus.DOWN
        assert report.down_count == 2
        assert report.healthy_count == 1
        assert report.alerts_emitted == 2
        assert report.restarts_attempted == 2
        # First restart succeeded, second failed
        assert report.checks[0].restart_succeeded is True
        assert report.checks[1].restart_succeeded is False

    async def test_event_bus_wiring(self, event_bus: EventBusInmemory) -> None:
        """Handler events can be wired through EventBusInmemory."""
        handler = HandlerProcessWatchdog()
        completed_events: list[dict[str, object]] = []

        async def on_command(message: object) -> None:
            payload = json.loads(message.value)  # type: ignore[union-attr]
            command = ModelWatchdogStartCommand(
                check_targets=payload.get(
                    "check_targets",
                    [t.value for t in EnumCheckTarget],
                ),
                correlation_id=payload["correlation_id"],
                requested_at=datetime.now(tz=UTC),
            )
            targets = _healthy_targets()
            _report, completed = handler.run_watchdog(command, targets)
            completed_payload = completed.model_dump(mode="json")
            completed_events.append(completed_payload)
            await event_bus.publish(
                COMPLETED_TOPIC,
                key=None,
                value=json.dumps(completed_payload).encode(),
            )

        await event_bus.start()
        await event_bus.subscribe(
            CMD_TOPIC,
            on_message=on_command,
            group_id="test-process-watchdog",
        )

        cmd_payload = json.dumps(
            {
                "correlation_id": str(uuid4()),
            }
        ).encode()
        await event_bus.publish(CMD_TOPIC, key=None, value=cmd_payload)

        assert len(completed_events) == 1
        assert completed_events[0]["overall_status"] == "healthy"

        await event_bus.close()

    async def test_unknown_status_aggregation(
        self, event_bus: EventBusInmemory
    ) -> None:
        """UNKNOWN status is worse than HEALTHY but better than DEGRADED."""
        handler = HandlerProcessWatchdog()
        targets = [
            InmemoryCheckTarget(
                name="emit_daemon_socket",
                category=EnumCheckTarget.EMIT_DAEMON,
                status=EnumCheckStatus.HEALTHY,
            ),
            InmemoryCheckTarget(
                name="kafka_check",
                category=EnumCheckTarget.KAFKA_CONSUMERS,
                status=EnumCheckStatus.UNKNOWN,
                message="Could not connect to check",
            ),
        ]
        command = _make_command()

        report, _completed = handler.run_watchdog(command, targets)

        assert report.overall_status == EnumCheckStatus.UNKNOWN
        assert report.unknown_count == 1
        assert report.healthy_count == 1

    async def test_empty_targets_returns_unknown(
        self, event_bus: EventBusInmemory
    ) -> None:
        """No targets at all -> UNKNOWN overall status."""
        handler = HandlerProcessWatchdog()
        command = _make_command()

        report, completed = handler.run_watchdog(command, [])

        assert report.overall_status == EnumCheckStatus.UNKNOWN
        assert report.total_checks == 0
        assert completed.overall_status == EnumCheckStatus.UNKNOWN
