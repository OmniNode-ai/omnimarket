"""Golden chain test for node_platform_readiness.

Verifies the readiness gate logic with freshness-aware semantics.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_platform_readiness.__main__ import (
    main as readiness_cli_main,
)
from omnimarket.nodes.node_platform_readiness.handlers.handler_platform_readiness import (
    DimensionInput,
    NodePlatformReadiness,
    PlatformReadinessRequest,
    ReadinessStatus,
)

CMD_TOPIC = "onex.cmd.omnimarket.platform-readiness-start.v1"
EVT_TOPIC = "onex.evt.omnimarket.platform-readiness-completed.v1"
DRY_RUN_TIMESTAMP = "2025-01-01T00:00:00Z"


@pytest.mark.unit
class TestPlatformReadinessGoldenChain:
    """Golden chain: command -> evaluate -> completion event."""

    async def test_all_healthy_dimensions_pass(
        self, event_bus: EventBusInmemory
    ) -> None:
        """All healthy, fresh dimensions should produce overall PASS."""
        handler = NodePlatformReadiness()
        now = datetime.now(UTC)
        dims = [
            DimensionInput(
                name="contract_completeness",
                critical=True,
                healthy=True,
                last_checked=now - timedelta(hours=1),
                details="7/7 contracts complete",
            ),
            DimensionInput(
                name="ci_health",
                critical=True,
                healthy=True,
                last_checked=now - timedelta(minutes=30),
                details="All workflows green",
            ),
        ]
        request = PlatformReadinessRequest(dimensions=dims, now=now)
        result = handler.handle(request)

        assert result.overall == ReadinessStatus.PASS
        assert len(result.blockers) == 0
        assert len(result.degraded) == 0

    async def test_stale_data_produces_warn(self, event_bus: EventBusInmemory) -> None:
        """Data older than 24h should produce WARN."""
        handler = NodePlatformReadiness()
        now = datetime.now(UTC)
        dims = [
            DimensionInput(
                name="data_flow_health",
                critical=False,
                healthy=True,
                last_checked=now - timedelta(hours=30),
                details="Last sweep passed",
            ),
        ]
        request = PlatformReadinessRequest(dimensions=dims, now=now)
        result = handler.handle(request)

        assert result.overall == ReadinessStatus.WARN
        assert len(result.degraded) == 1
        assert "data_flow_health" in result.degraded[0]

    async def test_missing_data_produces_fail(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Data older than 72h should produce FAIL."""
        handler = NodePlatformReadiness()
        now = datetime.now(UTC)
        dims = [
            DimensionInput(
                name="golden_chain_health",
                critical=True,
                healthy=True,
                last_checked=now - timedelta(hours=80),
                details="Last sweep passed long ago",
            ),
        ]
        request = PlatformReadinessRequest(dimensions=dims, now=now)
        result = handler.handle(request)

        assert result.overall == ReadinessStatus.FAIL
        assert len(result.blockers) == 1

    async def test_mock_data_always_fails(self, event_bus: EventBusInmemory) -> None:
        """Mock data detected should always produce FAIL."""
        handler = NodePlatformReadiness()
        now = datetime.now(UTC)
        dims = [
            DimensionInput(
                name="dashboard_data",
                critical=False,
                healthy=True,
                last_checked=now,
                details="Fake data",
                is_mock=True,
            ),
        ]
        request = PlatformReadinessRequest(dimensions=dims, now=now)
        result = handler.handle(request)

        assert result.overall == ReadinessStatus.FAIL
        assert any("mock" in b.lower() for b in result.blockers)

    async def test_unhealthy_dimension_fails(self, event_bus: EventBusInmemory) -> None:
        """An unhealthy dimension with fresh data should FAIL."""
        handler = NodePlatformReadiness()
        now = datetime.now(UTC)
        dims = [
            DimensionInput(
                name="ci_health",
                critical=True,
                healthy=False,
                last_checked=now - timedelta(minutes=10),
                details="2 failing workflows",
            ),
        ]
        request = PlatformReadinessRequest(dimensions=dims, now=now)
        result = handler.handle(request)

        assert result.overall == ReadinessStatus.FAIL
        assert len(result.blockers) == 1

    async def test_missing_dimension_data(self, event_bus: EventBusInmemory) -> None:
        """A dimension with no data should FAIL."""
        handler = NodePlatformReadiness()
        now = datetime.now(UTC)
        dims = [
            DimensionInput(
                name="runtime_wiring",
                critical=False,
                healthy=None,
                last_checked=None,
                details="",
            ),
        ]
        request = PlatformReadinessRequest(dimensions=dims, now=now)
        result = handler.handle(request)

        assert result.overall == ReadinessStatus.FAIL

    async def test_event_bus_wiring(self, event_bus: EventBusInmemory) -> None:
        """Handler can be wired to event bus for command/completion flow."""
        handler = NodePlatformReadiness()
        completions: list[dict[str, object]] = []
        now = datetime.now(UTC)

        async def on_command(message: object) -> None:
            dims = [
                DimensionInput(
                    name="ci_health",
                    critical=True,
                    healthy=True,
                    last_checked=now,
                    details="All green",
                ),
            ]
            request = PlatformReadinessRequest(dimensions=dims, now=now)
            result = handler.handle(request)
            completion = {
                "overall": result.overall.value,
                "blockers": len(result.blockers),
                "degraded": len(result.degraded),
            }
            completions.append(completion)
            await event_bus.publish(
                EVT_TOPIC,
                key=None,
                value=json.dumps(completion).encode(),
            )

        await event_bus.start()
        await event_bus.subscribe(
            CMD_TOPIC, on_message=on_command, group_id="test-readiness"
        )

        await event_bus.publish(CMD_TOPIC, key=None, value=b'{"check": "all"}')

        assert len(completions) == 1
        assert completions[0]["overall"] == "PASS"

        history = await event_bus.get_event_history(topic=EVT_TOPIC)
        assert len(history) == 1

        await event_bus.close()

    def test_module_cli_dry_run_outputs_valid_json(self) -> None:
        """The local module runner returns readiness JSON without Kafka."""
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "omnimarket.nodes.node_platform_readiness",
                "--dry-run",
                "--output-format",
                "json",
            ],
            check=True,
            capture_output=True,
            text=True,
        )

        payload = json.loads(completed.stdout)
        assert payload["overall"] == "PASS"
        assert len(payload["dimensions"]) == 7
        assert payload["blockers"] == []
        assert payload["timestamp"] == DRY_RUN_TIMESTAMP

    def test_module_cli_dry_run_output_is_deterministic(self) -> None:
        """Dry-run output should be stable enough for receipt evidence."""
        command = [
            sys.executable,
            "-m",
            "omnimarket.nodes.node_platform_readiness",
            "--dry-run",
            "--output-format",
            "json",
        ]

        first = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
        )
        second = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
        )

        assert first.stdout == second.stdout

    def test_cli_runtime_value_error_is_not_reported_as_bad_dimension(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only dimension validation ValueErrors should become CLI usage errors."""

        def raise_runtime_value_error(_self: object, _request: object) -> object:
            raise ValueError("handler failure")

        monkeypatch.setattr(
            "omnimarket.nodes.node_platform_readiness.__main__."
            "NodePlatformReadiness.handle",
            raise_runtime_value_error,
        )

        with pytest.raises(ValueError, match="handler failure"):
            readiness_cli_main(["--dry-run", "--dimension", "plugin_version"])

    def test_module_cli_filters_dimension(self) -> None:
        """The module runner can narrow the readiness report to one dimension."""
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "omnimarket.nodes.node_platform_readiness",
                "--dry-run",
                "--output-format",
                "json",
                "--dimension",
                "kafka_topic_coverage",
            ],
            check=True,
            capture_output=True,
            text=True,
        )

        payload = json.loads(completed.stdout)
        assert payload["overall"] == "PASS"
        assert [dim["name"] for dim in payload["dimensions"]] == [
            "kafka_topic_coverage"
        ]
