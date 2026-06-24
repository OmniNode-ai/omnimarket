"""Golden chain tests for node_create_followup_tickets_effect."""

from __future__ import annotations

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from pydantic import ValidationError

from omnimarket.nodes.node_create_followup_tickets_effect.handlers.handler_create_followup_tickets_effect import (
    HandlerCreateFollowupTicketsEffect,
)
from omnimarket.nodes.node_create_followup_tickets_effect.models.model_create_followup_tickets_state import (
    EnumFindingSeverity,
    ModelCreateFollowupTicketsCommand,
    ModelReviewFinding,
)

CMD_TOPIC = "onex.cmd.omnimarket.create-followup-tickets-start.v1"
EVT_TOPIC = "onex.evt.omnimarket.create-followup-tickets-completed.v1"


class _RecordingAdapter:
    """In-test Linear adapter that records payloads and mints sequential IDs."""

    def __init__(self) -> None:
        self.payloads: list[dict[str, object]] = []

    def create_ticket(self, payload: dict[str, object]) -> dict[str, str]:
        self.payloads.append(payload)
        ticket_id = f"OMN-{9000 + len(self.payloads)}"
        return {
            "ticket_id": ticket_id,
            "ticket_url": f"https://linear.app/omninode/issue/{ticket_id}",
        }


@pytest.mark.unit
class TestCreateFollowupTicketsEffectGoldenChain:
    """Golden chain tests: models, stub handler, event bus wiring."""

    def test_severity_enum_values(self) -> None:
        """All four severity levels are defined with expected string values."""
        assert EnumFindingSeverity.CRITICAL == "critical"
        assert EnumFindingSeverity.MAJOR == "major"
        assert EnumFindingSeverity.MINOR == "minor"
        assert EnumFindingSeverity.NIT == "nit"

    def test_review_finding_model_valid(self) -> None:
        """A fully specified ModelReviewFinding round-trips through Pydantic."""
        finding = ModelReviewFinding(
            severity=EnumFindingSeverity.MAJOR,
            description="Missing password validation in auth flow.",
            file_path="src/auth.py",
            line_number=89,
            keyword="missing validation",
        )
        assert finding.severity == EnumFindingSeverity.MAJOR
        assert finding.file_path == "src/auth.py"
        assert finding.line_number == 89

    def test_review_finding_optional_fields_default_none(self) -> None:
        """file_path, line_number, and keyword default to None."""
        finding = ModelReviewFinding(
            severity=EnumFindingSeverity.MINOR,
            description="Magic number should be a constant.",
        )
        assert finding.file_path is None
        assert finding.line_number is None
        assert finding.keyword is None

    def test_command_model_defaults(self) -> None:
        """ModelCreateFollowupTicketsCommand has sensible defaults."""
        cmd = ModelCreateFollowupTicketsCommand()
        assert cmd.team == "Omninode"
        assert cmd.include_nits is False
        assert cmd.dry_run is False
        assert cmd.findings == ()

    def test_command_model_with_findings(self) -> None:
        """Command model accepts a tuple of findings."""
        findings = (
            ModelReviewFinding(
                severity=EnumFindingSeverity.CRITICAL,
                description="SQL injection vulnerability.",
                file_path="src/api.py",
                line_number=45,
            ),
            ModelReviewFinding(
                severity=EnumFindingSeverity.MAJOR,
                description="Unhandled exception in payment flow.",
                file_path="src/payments.py",
                line_number=112,
            ),
        )
        cmd = ModelCreateFollowupTicketsCommand(
            correlation_id="test-corr-001",
            source_review_id="review-abc",
            findings=findings,
            project="beta hardening",
            repo="omnimarket",
        )
        assert len(cmd.findings) == 2
        assert cmd.findings[0].severity == EnumFindingSeverity.CRITICAL
        assert cmd.project == "beta hardening"

    def test_command_model_is_frozen(self) -> None:
        """ModelCreateFollowupTicketsCommand is immutable (frozen=True)."""
        cmd = ModelCreateFollowupTicketsCommand(correlation_id="abc")
        with pytest.raises(ValidationError):
            cmd.correlation_id = "mutated"  # type: ignore[misc]

    def test_handler_dry_run_returns_preview_tickets(self) -> None:
        """Dry-run mode returns deterministic ticket refs without an adapter."""
        handler = HandlerCreateFollowupTicketsEffect()
        cmd = ModelCreateFollowupTicketsCommand(
            correlation_id="corr-1",
            dry_run=True,
            findings=(
                ModelReviewFinding(
                    severity=EnumFindingSeverity.MAJOR,
                    description="Some review finding.",
                ),
                ModelReviewFinding(
                    severity=EnumFindingSeverity.NIT,
                    description="Formatting nit.",
                ),
            ),
        )

        result = handler.handle(cmd)

        assert result.status == "dry_run"
        assert result.correlation_id == "corr-1"
        assert [ticket.ticket_id for ticket in result.created_tickets] == ["DRY-RUN-1"]
        assert result.skipped_nit_count == 1
        assert result.failures == ()

    def test_handler_without_adapter_fails_safely(self) -> None:
        """Live mode does not call external services without an adapter."""
        handler = HandlerCreateFollowupTicketsEffect()
        result = handler.handle(
            ModelCreateFollowupTicketsCommand(
                findings=(
                    ModelReviewFinding(
                        severity=EnumFindingSeverity.MAJOR,
                        description="Some review finding.",
                    ),
                )
            )
        )

        assert result.status == "error"
        assert result.created_tickets == ()
        assert (
            result.failures[0].reason == "linear adapter required when dry_run is false"
        )

    def test_handler_creates_two_tickets_from_mock_findings(self) -> None:
        """Live mode creates one ticket per finding via an injected adapter.

        DoD golden chain: 2 mock findings → 2 created Linear tickets, with
        priority derived from severity and labels carrying the repo + keyword.
        """
        adapter = _RecordingAdapter()
        handler = HandlerCreateFollowupTicketsEffect(adapter=adapter)
        cmd = ModelCreateFollowupTicketsCommand(
            correlation_id="corr-2tickets",
            source_review_id="review-xyz",
            repo="omnimarket",
            parent="OMN-12279",
            findings=(
                ModelReviewFinding(
                    severity=EnumFindingSeverity.CRITICAL,
                    description="SQL injection vulnerability.",
                    file_path="src/api.py",
                    line_number=45,
                    keyword="sql injection",
                ),
                ModelReviewFinding(
                    severity=EnumFindingSeverity.MAJOR,
                    description="Unhandled exception in payment flow.",
                    file_path="src/payments.py",
                    line_number=112,
                    keyword="error handling",
                ),
            ),
        )

        result = handler.handle(cmd)

        assert result.status == "created"
        assert len(result.created_tickets) == 2
        assert result.failures == ()
        assert [t.ticket_id for t in result.created_tickets] == ["OMN-9001", "OMN-9002"]
        assert [t.finding_index for t in result.created_tickets] == [0, 1]
        # Two distinct adapter calls, priorities mapped from severity.
        assert len(adapter.payloads) == 2
        assert adapter.payloads[0]["priority"] == 1  # CRITICAL → 1
        assert adapter.payloads[1]["priority"] == 2  # MAJOR → 2
        assert adapter.payloads[0]["parent"] == "OMN-12279"
        assert "omnimarket" in adapter.payloads[0]["labels"]
        assert "sql injection" in adapter.payloads[0]["labels"]

    def test_handler_skips_duplicate_titles_idempotently(self) -> None:
        """Findings producing an identical title are deduplicated by title match.

        Two findings with the same severity + description yield the same ticket
        title; only the first is created, the rest are counted as duplicates.
        """
        adapter = _RecordingAdapter()
        handler = HandlerCreateFollowupTicketsEffect(adapter=adapter)
        duplicate_finding = ModelReviewFinding(
            severity=EnumFindingSeverity.MAJOR,
            description="Unhandled exception in payment flow.",
        )
        cmd = ModelCreateFollowupTicketsCommand(
            correlation_id="corr-dedup",
            findings=(
                duplicate_finding,
                ModelReviewFinding(
                    severity=EnumFindingSeverity.CRITICAL,
                    description="SQL injection vulnerability.",
                ),
                duplicate_finding,  # same title as the first finding
            ),
        )

        result = handler.handle(cmd)

        assert result.status == "created"
        assert len(result.created_tickets) == 2
        assert result.skipped_duplicate_count == 1
        assert len(adapter.payloads) == 2  # adapter only called for unique titles

    def test_dry_run_dedup_is_deterministic(self) -> None:
        """Dry-run mode also skips duplicate titles without calling an adapter."""
        handler = HandlerCreateFollowupTicketsEffect()
        finding = ModelReviewFinding(
            severity=EnumFindingSeverity.MINOR,
            description="Magic number should be a constant.",
        )
        cmd = ModelCreateFollowupTicketsCommand(
            dry_run=True,
            findings=(finding, finding),
        )

        result = handler.handle(cmd)

        assert result.status == "dry_run"
        assert len(result.created_tickets) == 1
        assert result.skipped_duplicate_count == 1

    async def test_event_bus_topics_defined(self, event_bus: EventBusInmemory) -> None:
        """CMD and EVT topics conform to the onex topic naming convention."""
        assert CMD_TOPIC.startswith("onex.cmd.")
        assert EVT_TOPIC.startswith("onex.evt.")
        assert CMD_TOPIC.endswith(".v1")
        assert EVT_TOPIC.endswith(".v1")

        # Verify the in-memory bus can publish/subscribe to both topics
        received: list[object] = []

        async def capture(msg: object) -> None:
            received.append(msg)

        await event_bus.start()
        await event_bus.subscribe(CMD_TOPIC, on_message=capture, group_id="test-cfte")
        await event_bus.publish(CMD_TOPIC, key=None, value=b"ping")

        assert len(received) == 1
        await event_bus.close()
