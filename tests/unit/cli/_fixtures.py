from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import yaml

from omnimarket.models.cli_report import (
    EnumMarketCliOutputFormat,
    EnumMarketCliStatus,
    EnumMarketCliVerbosity,
    ModelMarketCliEvidenceRef,
    ModelMarketCliInputSummary,
    ModelMarketCliOutputConfig,
    ModelMarketCliReport,
    ModelMarketCliStep,
)


def _ticket_pipeline_terminal_event() -> str:
    contract_path = (
        Path(__file__).resolve().parents[3]
        / "src"
        / "omnimarket"
        / "nodes"
        / "node_ticket_pipeline"
        / "contract.yaml"
    )
    raw = yaml.safe_load(contract_path.read_text())
    return str(raw["terminal_event"])


def make_sample_report(
    *,
    verbosity: EnumMarketCliVerbosity = EnumMarketCliVerbosity.STANDARD,
) -> ModelMarketCliReport:
    return ModelMarketCliReport(
        skill_name="ticket_pipeline",
        node_name="node_ticket_pipeline",
        contract_name="ticket_pipeline",
        contract_version="1.0.0",
        run_id=UUID("11111111-1111-4111-8111-111111111111"),
        correlation_id=UUID("22222222-2222-4222-8222-222222222222"),
        mode="dry_run",
        status=EnumMarketCliStatus.BLOCKED,
        input_summary=ModelMarketCliInputSummary(fields={"ticket_id": "OMN-9530"}),
        steps=[
            ModelMarketCliStep(
                name="pre_flight",
                status="succeeded",
                description="validated command envelope",
                details={"validator": "command_envelope"},
            ),
            ModelMarketCliStep(
                name="local_review",
                status="blocked",
                description="implementation pending",
                details={"stop_reason": "not_implemented"},
            ),
        ],
        evidence=[
            ModelMarketCliEvidenceRef(
                kind="contract",
                ref="src/omnimarket/nodes/node_ticket_pipeline/contract.yaml",
                description="terminal event source",
            )
        ],
        result_summary={"stop_reason": "not_implemented"},
        terminal_event=_ticket_pipeline_terminal_event(),
        output_config=ModelMarketCliOutputConfig(
            format=EnumMarketCliOutputFormat.JSON,
            verbosity=verbosity,
        ),
        started_at=datetime(2026, 4, 30, 14, 0, 0, tzinfo=UTC),
        completed_at=datetime(2026, 4, 30, 14, 0, 0, 42000, tzinfo=UTC),
        duration_ms=42,
    )
