"""Golden chain tests for node_data_verification.

Verifies the compute node: start command -> data quality checks -> completion event,
various check outcomes, and EventBusInmemory wiring.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_data_verification.handlers.handler_data_verification import (
    HandlerDataVerification,
    InmemoryDataSource,
)
from omnimarket.nodes.node_data_verification.models.model_data_verification_start_command import (
    ModelDataVerificationStartCommand,
)
from omnimarket.nodes.node_data_verification.models.model_data_verification_state import (
    EnumDataCheck,
    EnumVerificationStatus,
)

CMD_TOPIC = "onex.cmd.omnimarket.data-verification-start.v1"
COMPLETED_TOPIC = "onex.evt.omnimarket.data-verification-completed.v1"


def _make_command(
    table_name: str = "platform_nodes",
    expected_columns: list[str] | None = None,
    unique_columns: list[str] | None = None,
    uuid_columns: list[str] | None = None,
    min_rows: int = 1,
    sample_size: int = 3,
    topic_name: str | None = None,
    dry_run: bool = False,
) -> ModelDataVerificationStartCommand:
    return ModelDataVerificationStartCommand(
        table_name=table_name,
        topic_name=topic_name,
        test_event_payload=None,
        expected_columns=expected_columns or [],
        unique_columns=unique_columns or [],
        uuid_columns=uuid_columns or [],
        min_rows=min_rows,
        sample_size=sample_size,
        correlation_id=str(uuid4()),
        dry_run=dry_run,
        requested_at=datetime.now(tz=UTC),
    )


def _clean_rows(count: int = 3) -> list[dict[str, str]]:
    """Generate clean rows with valid UUID v4 and populated fields."""
    return [
        {
            "id": str(uuid4()),
            "name": f"node_{i}",
            "status": "active",
            "created_at": "2026-04-01T00:00:00Z",
        }
        for i in range(count)
    ]


@pytest.mark.unit
class TestDataVerificationGoldenChain:
    """Golden chain: start command -> data checks -> completion."""

    async def test_all_checks_pass_clean_data(
        self, event_bus: EventBusInmemory
    ) -> None:
        """All checks pass on clean data -> PASS."""
        handler = HandlerDataVerification()
        rows = _clean_rows(3)
        data_source = InmemoryDataSource(rows)
        command = _make_command(
            expected_columns=["id", "name", "status"],
            unique_columns=["id"],
            uuid_columns=["id"],
        )

        result, completed = handler.run_verification(command, data_source)

        assert result.status == EnumVerificationStatus.PASS
        assert result.total_rows == 3
        assert len(result.sample_rows) == 3
        assert len(result.issues) == 0
        assert completed.status == EnumVerificationStatus.PASS
        assert completed.table_name == "platform_nodes"

    async def test_garbage_uuid_detected(self, event_bus: EventBusInmemory) -> None:
        """Garbage UUID (nil) detected -> issues reported."""
        handler = HandlerDataVerification()
        rows = [
            {
                "id": "00000000-0000-0000-0000-000000000000",
                "name": "bad_node",
                "status": "active",
            },
        ]
        data_source = InmemoryDataSource(rows)
        command = _make_command(
            uuid_columns=["id"],
            expected_columns=["name"],
        )

        result, _completed = handler.run_verification(command, data_source)

        assert result.status != EnumVerificationStatus.PASS
        assert any("garbage UUID" in issue for issue in result.issues)
        row = result.sample_rows[0]
        assert EnumDataCheck.NO_GARBAGE_UUIDS in row.checks_failed

    async def test_null_required_field_detected(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Null required field detected -> issues reported."""
        handler = HandlerDataVerification()
        rows = [
            {"id": str(uuid4()), "name": "", "status": "active"},
        ]
        data_source = InmemoryDataSource(rows)
        command = _make_command(
            expected_columns=["id", "name", "status"],
        )

        result, _completed = handler.run_verification(command, data_source)

        assert result.status != EnumVerificationStatus.PASS
        assert any("null/empty" in issue for issue in result.issues)
        row = result.sample_rows[0]
        assert EnumDataCheck.NO_NULL_REQUIRED_FIELDS in row.checks_failed

    async def test_duplicate_row_detected(self, event_bus: EventBusInmemory) -> None:
        """Duplicate values in unique columns detected."""
        handler = HandlerDataVerification()
        shared_id = str(uuid4())
        rows = [
            {"id": shared_id, "name": "node_a", "status": "active"},
            {"id": shared_id, "name": "node_b", "status": "active"},
        ]
        data_source = InmemoryDataSource(rows)
        command = _make_command(
            unique_columns=["id"],
            min_rows=1,
        )

        result, _completed = handler.run_verification(command, data_source)

        assert result.status != EnumVerificationStatus.PASS
        assert any("duplicate" in issue for issue in result.issues)

    async def test_row_count_below_minimum(self, event_bus: EventBusInmemory) -> None:
        """Row count below minimum -> issues reported."""
        handler = HandlerDataVerification()
        rows = _clean_rows(2)
        data_source = InmemoryDataSource(rows)
        command = _make_command(min_rows=5)

        result, _completed = handler.run_verification(command, data_source)

        assert result.status != EnumVerificationStatus.PASS
        assert any("Row count" in issue for issue in result.issues)

    async def test_event_publish_land_verification(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Event landed flag propagated in result."""
        handler = HandlerDataVerification()
        rows = _clean_rows(1)
        data_source = InmemoryDataSource(rows)
        command = _make_command(topic_name="onex.cmd.test.v1")

        result, _completed = handler.run_verification(
            command, data_source, event_landed=True, latency_ms=42.5
        )

        assert result.event_landed is True
        assert result.latency_ms == 42.5
        assert result.checks_summary[EnumDataCheck.EVENT_LANDED] == 1

    async def test_dry_run_mode(self, event_bus: EventBusInmemory) -> None:
        """dry_run flag propagates through result."""
        handler = HandlerDataVerification()
        rows = _clean_rows(1)
        data_source = InmemoryDataSource(rows)
        command = _make_command(dry_run=True)

        result, completed = handler.run_verification(command, data_source)

        assert result.dry_run is True
        assert completed.result.dry_run is True

    async def test_sample_size_respected(self, event_bus: EventBusInmemory) -> None:
        """Only sample_size rows are checked even if more exist."""
        handler = HandlerDataVerification()
        rows = _clean_rows(10)
        data_source = InmemoryDataSource(rows)
        command = _make_command(sample_size=2)

        result, _completed = handler.run_verification(command, data_source)

        assert len(result.sample_rows) == 2
        assert result.total_rows == 10

    async def test_multiple_issues_one_verification(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Multiple issues in a single verification run."""
        handler = HandlerDataVerification()
        rows = [
            {
                "id": "00000000-0000-0000-0000-000000000000",
                "name": "",
                "status": "active",
            },
        ]
        data_source = InmemoryDataSource(rows)
        command = _make_command(
            expected_columns=["id", "name"],
            uuid_columns=["id"],
        )

        result, _completed = handler.run_verification(command, data_source)

        assert result.status != EnumVerificationStatus.PASS
        assert len(result.issues) >= 2
        assert any("garbage UUID" in i for i in result.issues)
        assert any("null/empty" in i for i in result.issues)

    async def test_event_bus_wiring(self, event_bus: EventBusInmemory) -> None:
        """Handler events can be wired through EventBusInmemory."""
        handler = HandlerDataVerification()
        completed_events: list[dict[str, object]] = []

        async def on_command(message: object) -> None:
            payload = json.loads(message.value)  # type: ignore[union-attr]
            command = ModelDataVerificationStartCommand(
                table_name=payload["table_name"],
                expected_columns=payload.get("expected_columns", []),
                unique_columns=payload.get("unique_columns", []),
                uuid_columns=payload.get("uuid_columns", []),
                correlation_id=payload["correlation_id"],
                requested_at=datetime.now(tz=UTC),
            )
            rows = _clean_rows(3)
            data_source = InmemoryDataSource(rows)
            _result, completed = handler.run_verification(command, data_source)
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
            group_id="test-data-verification",
        )

        cmd_payload = json.dumps(
            {
                "table_name": "platform_nodes",
                "correlation_id": str(uuid4()),
                "expected_columns": ["id", "name"],
            }
        ).encode()
        await event_bus.publish(CMD_TOPIC, key=None, value=cmd_payload)

        assert len(completed_events) == 1
        assert completed_events[0]["status"] == "pass"
        assert completed_events[0]["table_name"] == "platform_nodes"

        await event_bus.close()

    async def test_empty_table_fails_row_count(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Empty table returns FAIL with ROW_COUNT_NONZERO issue."""
        handler = HandlerDataVerification()
        data_source = InmemoryDataSource([])
        command = _make_command(min_rows=1)

        result, completed = handler.run_verification(command, data_source)

        assert result.status == EnumVerificationStatus.FAIL
        assert result.total_rows == 0
        assert len(result.sample_rows) == 0
        assert any("Row count" in issue for issue in result.issues)
        assert completed.status == EnumVerificationStatus.FAIL
