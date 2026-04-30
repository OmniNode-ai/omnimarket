"""JSON renderer for typed CLI reports."""

from __future__ import annotations

from omnimarket.models.cli_report import ModelMarketCliReport


class HandlerCliOutputJson:
    """Render CLI reports as indented JSON."""

    format_name: str = "json"

    def render(self, report: ModelMarketCliReport) -> str:
        """Render a report as parseable JSON with no log prefixes."""
        return report.model_dump_json(indent=2)
