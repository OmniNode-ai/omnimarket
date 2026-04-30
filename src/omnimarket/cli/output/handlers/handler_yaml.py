"""YAML renderer for typed CLI reports."""

from __future__ import annotations

import json
from typing import Any

import yaml

from omnimarket.models.cli_report import ModelMarketCliReport


class HandlerCliOutputYaml:
    """Render CLI reports as safe YAML."""

    format_name: str = "yaml"

    def render(self, report: ModelMarketCliReport) -> str:
        """Render a report as safe YAML without Python-specific tags."""
        plain: Any = json.loads(report.model_dump_json())
        return yaml.safe_dump(plain, sort_keys=False, default_flow_style=False)
