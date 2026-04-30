"""Markdown renderer for typed CLI reports."""

from __future__ import annotations

from omnimarket.models.cli_report import ModelMarketCliReport


def _cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


class HandlerCliOutputMarkdown:
    """Render CLI reports as plain markdown."""

    format_name: str = "markdown"

    def render(self, report: ModelMarketCliReport) -> str:
        """Render a report as plain markdown with no external calls."""
        lines = [
            f"# Skill: {report.skill_name}",
            "",
            "| Field | Value |",
            "| --- | --- |",
            f"| Skill | {_cell(report.skill_name)} |",
            f"| Node | {_cell(report.node_name)} |",
            f"| Contract | {_cell(report.contract_name)} v{_cell(report.contract_version)} |",
            f"| Mode | {_cell(report.mode)} |",
            f"| Status | {_cell(report.status.value)} |",
            f"| Run ID | {_cell(report.run_id)} |",
            "",
            "## Execution",
            "",
            "| Step | Status | Description |",
            "| --- | --- | --- |",
        ]
        if report.steps:
            for step in report.steps:
                lines.append(
                    f"| {_cell(step.name)} | {_cell(step.status)} | {_cell(step.description)} |"
                )
        else:
            lines.append("| none | skipped |  |")

        lines.extend(["", "## Evidence", ""])
        if report.evidence:
            for evidence in report.evidence:
                description = (
                    f" - {evidence.description}" if evidence.description else ""
                )
                lines.append(f"- {evidence.kind}: {evidence.ref}{description}")
        else:
            lines.append("- none")

        lines.extend(["", "## Result", ""])
        if report.result_summary:
            for key, value in report.result_summary.items():
                lines.append(f"- {key}: {_cell(value)}")
        else:
            lines.append("- none")
        lines.append(f"- terminal_event: {_cell(report.terminal_event)}")

        return "\n".join(lines) + "\n"
