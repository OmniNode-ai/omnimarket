# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Classifier seed harness — regression net for build-loop buildability.

Runs ``HandlerTicketClassify`` against known-good/known-bad/edge-case seed
tickets plus per-criterion coverage fixtures. If classifier strictness
regresses (e.g. the loop starts returning 0 buildable tickets again, as in
Blocker B of OMN-7621), one of these assertions flips before the regression
reaches production.

Golden-chain style: fully in-memory ``ModelTicketContract`` mocks, no Linear
API, no Kafka, no runtime rebuild.

Related:
    - OMN-9580 (this harness)
    - OMN-7621 (parent: Build Loop -> Ticket Pipeline Dispatch)
    - OMN-7622 / OMN-7720 (prior classifier strictness fixes)
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from omnimarket.enums.enum_buildability import EnumBuildability
from omnimarket.nodes.node_ticket_classify_compute.handlers.handler_ticket_classify import (
    HandlerTicketClassify,
)
from tests.classifier.fixtures.seed_tickets import (
    SEED_ARCHETYPES,
    SEED_CRITERIA_COVERAGE,
    SeedCase,
)


@pytest.fixture
def handler() -> HandlerTicketClassify:
    return HandlerTicketClassify()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "seed",
    SEED_ARCHETYPES,
    ids=[case.label for case in SEED_ARCHETYPES],
)
async def test_seed_archetypes_classify_as_expected(
    handler: HandlerTicketClassify, seed: SeedCase
) -> None:
    """Known-good / known-bad / edge-ambiguous each classify exactly as recorded."""
    result = await handler.handle(correlation_id=uuid4(), tickets=(seed.ticket,))
    assert len(result.classifications) == 1
    classification = result.classifications[0]
    assert classification.ticket_id == seed.ticket.ticket_id
    assert classification.buildability == seed.expected_buildability, (
        f"Seed {seed.label!r} expected {seed.expected_buildability} "
        f"but got {classification.buildability}. reason={classification.reason!r}"
    )
    assert classification.seam_source == seed.expected_seam_source


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "seed",
    SEED_CRITERIA_COVERAGE,
    ids=[case.criterion for case in SEED_CRITERIA_COVERAGE],
)
async def test_each_buildability_criterion_is_exercised(
    handler: HandlerTicketClassify, seed: SeedCase
) -> None:
    """Each of the 5 classifier criteria has at least one fixture proving it fires."""
    result = await handler.handle(correlation_id=uuid4(), tickets=(seed.ticket,))
    classification = result.classifications[0]
    assert classification.buildability == seed.expected_buildability, (
        f"Criterion {seed.criterion!r} regressed: expected "
        f"{seed.expected_buildability}, got {classification.buildability}. "
        f"reason={classification.reason!r}"
    )
    assert classification.seam_source == seed.expected_seam_source


def test_coverage_fixtures_span_all_five_criteria() -> None:
    """Meta-check: the coverage fixtures collectively exercise all 5 criteria."""
    criteria = {case.criterion for case in SEED_CRITERIA_COVERAGE}
    expected = {
        "contract_empty_consumes",
        "contract_all_mockable",
        "contract_unmockable",
        "keyword_skip",
        "keyword_auto_buildable",
    }
    assert criteria == expected, (
        f"Criterion coverage drifted. missing={expected - criteria}, "
        f"extra={criteria - expected}"
    )


@pytest.mark.asyncio
async def test_batch_classification_preserves_counts(
    handler: HandlerTicketClassify,
) -> None:
    """Classify all archetypes in one batch; totals match per-ticket expectations."""
    tickets = tuple(case.ticket for case in SEED_ARCHETYPES)
    result = await handler.handle(correlation_id=uuid4(), tickets=tickets)
    assert len(result.classifications) == len(SEED_ARCHETYPES)

    expected_auto = sum(
        1
        for case in SEED_ARCHETYPES
        if case.expected_buildability == EnumBuildability.AUTO_BUILDABLE
    )
    expected_non_auto = len(SEED_ARCHETYPES) - expected_auto
    assert result.total_auto_buildable == expected_auto
    assert result.total_non_buildable == expected_non_auto


@pytest.mark.asyncio
async def test_seed_harness_produces_at_least_one_buildable(
    handler: HandlerTicketClassify,
) -> None:
    """Blocker B regression net: full seed set must yield >= 1 AUTO_BUILDABLE.

    If the classifier ever again returns 0 buildable across the full seed
    corpus, this assertion fails *before* production sees the same symptom.
    """
    all_seeds = SEED_ARCHETYPES + SEED_CRITERIA_COVERAGE
    tickets = tuple(case.ticket for case in all_seeds)
    result = await handler.handle(correlation_id=uuid4(), tickets=tickets)
    assert result.total_auto_buildable >= 1, (
        "Classifier returned 0 AUTO_BUILDABLE across seed corpus — "
        "OMN-7621 Blocker B regression."
    )
