# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure single-attempt cohort reporting for availability and content (OMN-18931)."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from uuid import UUID

from omnibase_core.enums.enum_delegation_content_verdict import (
    EnumDelegationContentVerdict,
)
from omnibase_core.enums.enum_delegation_operational_outcome import (
    EnumDelegationOperationalOutcome,
)

from omnimarket.nodes.node_delegation_availability_report_compute.models.model_delegation_availability_report import (
    ModelDelegationAvailabilityReport,
)
from omnimarket.nodes.node_delegation_availability_report_compute.models.model_delegation_availability_request import (
    ModelDelegationAvailabilityRequest,
)
from omnimarket.nodes.node_delegation_availability_report_compute.models.model_delegation_availability_row import (
    AvailabilityOutcome,
    ModelDelegationAvailabilityRow,
)
from omnimarket.nodes.node_delegation_availability_report_compute.models.model_delegation_cohort_observation import (
    ModelDelegationCohortObservation,
)
from omnimarket.nodes.node_delegation_availability_report_compute.models.model_delegation_tier_report import (
    ModelDelegationTierReport,
)

_CONTENT_OUTCOMES = frozenset(
    {
        EnumDelegationOperationalOutcome.COMPLETED,
        EnumDelegationOperationalOutcome.SCHEMA_REJECTED,
        EnumDelegationOperationalOutcome.QUALITY_REJECTED,
    }
)
_CONTENT_VERDICTS = frozenset(
    {
        EnumDelegationContentVerdict.CORRECT,
        EnumDelegationContentVerdict.USABLE,
        EnumDelegationContentVerdict.UNUSABLE,
    }
)


def summarize_single_hop_cohort(
    observations: Sequence[ModelDelegationCohortObservation],
) -> ModelDelegationAvailabilityReport:
    """Report typed availability separately from evaluated final content.

    A missing terminal or zero attempts is operational evidence with no content
    verdict or score.
    Retried requests are excluded pending OMN-18916; duplicate requests are
    refused so a repeated receipt cannot change either denominator.
    """
    seen: set[UUID] = set()
    rows: list[ModelDelegationAvailabilityRow] = []
    excluded: list[UUID] = []
    for observation in observations:
        if observation.correlation_id in seen:
            raise ValueError("cohort contains a duplicate request")
        seen.add(observation.correlation_id)
        terminal = observation.terminal
        if observation.attempts_count > 1 or (
            terminal is not None and terminal.escalation_count != 0
        ):
            excluded.append(observation.correlation_id)
            continue
        outcome: AvailabilityOutcome = "no_terminal"
        verdict = None
        score = None
        if terminal is not None:
            assert terminal.operational_outcome is not None
            outcome = terminal.operational_outcome
            if terminal.content_verdict in _CONTENT_VERDICTS:
                verdict = terminal.content_verdict
                score = terminal.quality_score
        rows.append(
            ModelDelegationAvailabilityRow(
                correlation_id=observation.correlation_id,
                backend_tier=observation.backend_tier,
                availability=outcome,
                content_verdict=verdict,
                quality_score=score,
                usage_source=observation.usage.usage_source,
                estimation_method=observation.usage.estimation_method,
                source_payload_hash=observation.source_payload_hash,
            )
        )
    tiers = []
    for tier in sorted({row.backend_tier for row in rows}):
        tier_rows = [row for row in rows if row.backend_tier == tier]
        availability = Counter(row.availability for row in tier_rows)
        correctness = Counter(
            row.content_verdict for row in tier_rows if row.content_verdict is not None
        )
        tiers.append(
            ModelDelegationTierReport(
                backend_tier=tier,
                availability_total=len(tier_rows),
                availability_failures=sum(
                    count
                    for outcome, count in availability.items()
                    if outcome not in _CONTENT_OUTCOMES
                ),
                availability_counts=dict(availability),
                correctness_total=sum(correctness.values()),
                correctness_counts=dict(correctness),
            )
        )
    return ModelDelegationAvailabilityReport(
        rows=tuple(rows), tiers=tuple(tiers), excluded_requests=tuple(excluded)
    )


class HandlerDelegationAvailabilityReport:
    """Stateless report compute over caller-supplied fixed-cohort evidence."""

    def handle(
        self, request: ModelDelegationAvailabilityRequest
    ) -> ModelDelegationAvailabilityReport:
        return summarize_single_hop_cohort(request.observations)
