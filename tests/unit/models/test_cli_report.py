from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from omnimarket.models.cli_report import (
    EnumMarketCliOutputFormat,
    EnumMarketCliStatus,
    EnumMarketCliVerbosity,
    ModelMarketCliInputSummary,
    ModelMarketCliOutputConfig,
    ModelMarketCliReport,
    ModelMarketCliStep,
)


def test_report_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        ModelMarketCliReport.model_validate({"skill_name": "x", "extra_field": "boom"})


def test_report_round_trip_json() -> None:
    now = datetime.now(UTC)
    report = ModelMarketCliReport(
        skill_name="ticket_pipeline",
        node_name="node_ticket_pipeline",
        contract_name="ticket_pipeline",
        contract_version="1.0.0",
        run_id=uuid4(),
        correlation_id=uuid4(),
        mode="dry_run",
        status=EnumMarketCliStatus.BLOCKED,
        input_summary=ModelMarketCliInputSummary(fields={"ticket_id": "OMN-9530"}),
        steps=[
            ModelMarketCliStep(
                name="pre_flight",
                status="succeeded",
                description="validated command envelope",
            )
        ],
        evidence=[],
        result_summary={"stop_reason": "not_implemented"},
        terminal_event="onex.evt.omnimarket.ticket-pipeline-completed.v1",
        output_config=ModelMarketCliOutputConfig(
            format=EnumMarketCliOutputFormat.JSON,
            verbosity=EnumMarketCliVerbosity.STANDARD,
        ),
        started_at=now,
        completed_at=now,
        duration_ms=42,
    )
    raw = report.model_dump_json()
    parsed = ModelMarketCliReport.model_validate_json(raw)
    assert parsed.skill_name == "ticket_pipeline"
    assert parsed.status == EnumMarketCliStatus.BLOCKED
