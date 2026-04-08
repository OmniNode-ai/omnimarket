"""Golden chain tests for node_dod_verify.

Verifies the compute node: start command -> evidence checks -> completion event,
various check outcomes, and EventBusInmemory wiring.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_dod_verify.handlers.handler_dod_verify import (
    HandlerDodVerify,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_start_command import (
    ModelDodVerifyStartCommand,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumDodVerifyStatus,
    EnumEvidenceCheckStatus,
    ModelEvidenceCheckResult,
)

CMD_TOPIC = "onex.cmd.omnimarket.dod-verify-start.v1"
COMPLETED_TOPIC = "onex.evt.omnimarket.dod-verify-completed.v1"


def _make_command(
    ticket_id: str = "OMN-9999",
    dry_run: bool = False,
) -> ModelDodVerifyStartCommand:
    return ModelDodVerifyStartCommand(
        correlation_id=uuid4(),
        ticket_id=ticket_id,
        dry_run=dry_run,
        requested_at=datetime.now(tz=UTC),
    )


def _check(
    evidence_id: str,
    description: str,
    status: EnumEvidenceCheckStatus,
    message: str | None = None,
) -> ModelEvidenceCheckResult:
    return ModelEvidenceCheckResult(
        evidence_id=evidence_id,
        description=description,
        status=status,
        message=message,
    )


@pytest.mark.unit
class TestDodVerifyGoldenChain:
    """Golden chain: start command -> evidence checks -> completion."""

    async def test_all_checks_verified(self, event_bus: EventBusInmemory) -> None:
        """All evidence checks pass -> VERIFIED."""
        handler = HandlerDodVerify()
        command = _make_command()
        checks = [
            _check("dod-001", "Tests pass", EnumEvidenceCheckStatus.VERIFIED),
            _check("dod-002", "Config created", EnumEvidenceCheckStatus.VERIFIED),
        ]

        state, completed = handler.run_verification(command, checks)

        assert state.status == EnumDodVerifyStatus.VERIFIED
        assert state.total_checks == 2
        assert state.verified_count == 2
        assert state.failed_count == 0
        assert completed.status == EnumDodVerifyStatus.VERIFIED
        assert completed.ticket_id == "OMN-9999"

    async def test_some_checks_failed(self, event_bus: EventBusInmemory) -> None:
        """One check fails -> FAILED overall."""
        handler = HandlerDodVerify()
        command = _make_command()
        checks = [
            _check("dod-001", "Tests pass", EnumEvidenceCheckStatus.VERIFIED),
            _check(
                "dod-002",
                "Config created",
                EnumEvidenceCheckStatus.FAILED,
                "No files matching pattern",
            ),
        ]

        state, completed = handler.run_verification(command, checks)

        assert state.status == EnumDodVerifyStatus.FAILED
        assert state.verified_count == 1
        assert state.failed_count == 1
        assert completed.status == EnumDodVerifyStatus.FAILED

    async def test_no_evidence_items(self, event_bus: EventBusInmemory) -> None:
        """No evidence items -> SKIPPED."""
        handler = HandlerDodVerify()
        command = _make_command()

        state, completed = handler.run_verification(command, [])

        assert state.status == EnumDodVerifyStatus.SKIPPED
        assert state.total_checks == 0
        assert completed.status == EnumDodVerifyStatus.SKIPPED

    async def test_mixed_statuses(self, event_bus: EventBusInmemory) -> None:
        """Mix of verified, failed, and skipped checks."""
        handler = HandlerDodVerify()
        command = _make_command()
        checks = [
            _check("dod-001", "Tests pass", EnumEvidenceCheckStatus.VERIFIED),
            _check("dod-002", "Config", EnumEvidenceCheckStatus.FAILED, "missing"),
            _check("dod-003", "API health", EnumEvidenceCheckStatus.SKIPPED),
        ]

        state, _completed = handler.run_verification(command, checks)

        assert state.status == EnumDodVerifyStatus.FAILED
        assert state.verified_count == 1
        assert state.failed_count == 1
        assert state.skipped_count == 1
        assert state.total_checks == 3

    async def test_dry_run_propagated(self, event_bus: EventBusInmemory) -> None:
        """dry_run flag propagates through state."""
        handler = HandlerDodVerify()
        command = _make_command(dry_run=True)

        state, _completed = handler.run_verification(command)

        assert state.dry_run is True

    async def test_event_bus_wiring(self, event_bus: EventBusInmemory) -> None:
        """Handler events can be wired through EventBusInmemory."""
        handler = HandlerDodVerify()
        completed_events: list[dict[str, object]] = []

        async def on_command(message: object) -> None:
            payload = json.loads(message.value)  # type: ignore[union-attr]
            command = ModelDodVerifyStartCommand(
                correlation_id=payload["correlation_id"],
                ticket_id=payload["ticket_id"],
                dry_run=payload.get("dry_run", False),
                requested_at=datetime.now(tz=UTC),
            )
            checks = [
                ModelEvidenceCheckResult(
                    evidence_id="dod-001",
                    description="Tests pass",
                    status=EnumEvidenceCheckStatus.VERIFIED,
                )
            ]
            _state, completed = handler.run_verification(command, checks)
            completed_payload = completed.model_dump(mode="json")
            completed_events.append(completed_payload)
            await event_bus.publish(
                COMPLETED_TOPIC,
                key=None,
                value=json.dumps(completed_payload).encode(),
            )

        await event_bus.start()
        await event_bus.subscribe(
            CMD_TOPIC, on_message=on_command, group_id="test-dod-verify"
        )

        cmd_payload = json.dumps(
            {"correlation_id": str(uuid4()), "ticket_id": "OMN-1234"}
        ).encode()
        await event_bus.publish(CMD_TOPIC, key=None, value=cmd_payload)

        assert len(completed_events) == 1
        assert completed_events[0]["status"] == "verified"
        assert completed_events[0]["ticket_id"] == "OMN-1234"

        await event_bus.close()

    async def test_serialization(self, event_bus: EventBusInmemory) -> None:
        """Completed events serialize to valid JSON bytes."""
        handler = HandlerDodVerify()
        command = _make_command()
        checks = [
            _check("dod-001", "Tests pass", EnumEvidenceCheckStatus.VERIFIED),
        ]
        _, completed = handler.run_verification(command, checks)

        serialized = handler.serialize_completed(completed)
        deserialized = json.loads(serialized)
        assert deserialized["status"] == "verified"
        assert deserialized["ticket_id"] == "OMN-9999"
        assert len(deserialized["checks"]) == 1

    async def test_all_skipped_is_skipped(self, event_bus: EventBusInmemory) -> None:
        """All checks skipped -> overall SKIPPED (not VERIFIED)."""
        handler = HandlerDodVerify()
        command = _make_command()
        checks = [
            _check("dod-001", "API health", EnumEvidenceCheckStatus.SKIPPED),
            _check("dod-002", "Endpoint check", EnumEvidenceCheckStatus.SKIPPED),
        ]

        state, _completed = handler.run_verification(command, checks)

        # All checks skipped -> overall SKIPPED (not VERIFIED — nothing was verified)
        assert state.status == EnumDodVerifyStatus.SKIPPED
        assert state.skipped_count == 2
        assert state.failed_count == 0
