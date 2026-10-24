# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Synchronous, pure handler for the nightly technical deep dive."""

from __future__ import annotations

from typing import Literal

from omnimarket.nodes.node_deep_dive_report_effect.deep_dive.technical_report import (
    build_claims,
    render_technical_report,
)
from omnimarket.nodes.node_deep_dive_report_effect.models import (
    EnumReportSection,
    ModelTechnicalDeepDiveCommand,
    ModelTechnicalDeepDiveResult,
)


class HandlerTechnicalDeepDive:
    """Turn supplied source data into a report whose claims all cite evidence."""

    def handle(
        self, command: ModelTechnicalDeepDiveCommand
    ) -> ModelTechnicalDeepDiveResult:
        """Build and render claims, letting citation refusals reach the caller."""
        claims, uncited = build_claims(command)
        decisions_source: Literal["dropped-decisions.v1", "ledger-rulings"] = (
            "dropped-decisions.v1"
            if command.dropped_decisions is not None
            else "ledger-rulings"
        )
        errors = command.owner_summary_facts.get("errors", [])
        facts_errors = (
            [error for error in errors if isinstance(error, str)]
            if isinstance(errors, list)
            else []
        )
        markdown = render_technical_report(
            command.report_date, claims, uncited, decisions_source, facts_errors
        )
        return ModelTechnicalDeepDiveResult(
            correlation_id=command.correlation_id,
            report_date=command.report_date,
            report_markdown=markdown,
            claims=tuple(claims),
            uncited_source_items=uncited,
            decisions_source=decisions_source,
            counts={
                section.value: sum(claim.section == section for claim in claims)
                for section in EnumReportSection
            },
        )
