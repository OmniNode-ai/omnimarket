"""Protocol for CLI output report renderers."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from omnimarket.models.cli_report import ModelMarketCliReport


@runtime_checkable
class ProtocolCliOutputHandler(Protocol):
    """Renderer for one CLI report output format."""

    format_name: str

    def render(self, report: ModelMarketCliReport) -> str:
        """Render a CLI report as a string."""
        ...
