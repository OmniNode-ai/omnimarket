"""Plain text renderer for typed CLI reports."""

from __future__ import annotations

import json
from collections.abc import Mapping

from omnimarket.models.cli_report import (
    EnumMarketCliVerbosity,
    ModelMarketCliReport,
)


def _format_mapping(mapping: Mapping[str, object]) -> list[str]:
    if not mapping:
        return ["  none"]
    return [f"  {key}: {value}" for key, value in mapping.items()]


class HandlerCliOutputText:
    """Render CLI reports as stable plain text."""

    format_name: str = "text"

    def render(self, report: ModelMarketCliReport) -> str:
        """Render a report as readable text with no ANSI styling."""
        lines = [
            f"OMNIMARKET SKILL: {report.skill_name}",
            f"Node: {report.node_name}",
            f"Contract: {report.contract_name} v{report.contract_version}",
            f"Run ID: {report.run_id}",
            f"Correlation ID: {report.correlation_id}",
            f"Mode: {report.mode}",
            f"Status: {report.status.value}",
        ]
        if report.output_config.verbosity in {
            EnumMarketCliVerbosity.VERBOSE,
            EnumMarketCliVerbosity.DEBUG,
        }:
            lines.extend(
                [
                    f"started_at: {report.started_at.isoformat()}",
                    f"completed_at: {report.completed_at.isoformat()}",
                    f"duration_ms: {report.duration_ms}",
                ]
            )

        lines.extend(["", "Input"])
        lines.extend(_format_mapping(report.input_summary.fields))

        lines.extend(["", "Execution"])
        if report.steps:
            for step in report.steps:
                description = f" - {step.description}" if step.description else ""
                lines.append(f"  - {step.name}: {step.status}{description}")
                if (
                    report.output_config.verbosity
                    in {EnumMarketCliVerbosity.VERBOSE, EnumMarketCliVerbosity.DEBUG}
                    and step.details
                ):
                    detail_json = json.dumps(step.details, sort_keys=True)
                    lines.append(f"    details: {detail_json}")
        else:
            lines.append("  none")

        lines.extend(["", "Evidence"])
        if report.evidence:
            for evidence in report.evidence:
                description = (
                    f" - {evidence.description}" if evidence.description else ""
                )
                lines.append(f"  - {evidence.kind}: {evidence.ref}{description}")
        else:
            lines.append("  none")

        lines.extend(["", "Result"])
        lines.extend(_format_mapping(report.result_summary))
        lines.append(f"  terminal_event: {report.terminal_event}")

        return "\n".join(lines) + "\n"
