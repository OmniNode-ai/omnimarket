# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seed ticket fixtures for the build-loop classifier regression harness.

These fixtures are consumed by ``tests/classifier/test_seed_harness.py`` to
pin the observable classification behaviour of
``HandlerTicketClassify``. They are ``ModelTicketForClassification``
instances paired with the ``EnumBuildability`` value, keyword reason, and
``seam_source`` the classifier must return today.

Related:
    - OMN-7621 (parent plan — Blocker B step 5 regression net)
    - OMN-9580 (this harness)
"""

from __future__ import annotations

from dataclasses import dataclass

from omnimarket.enums.enum_buildability import EnumBuildability
from omnimarket.nodes.node_ticket_classify_compute.models.model_seam_boundaries import (
    ModelConsumedProtocol,
    ModelSeamBoundaries,
)
from omnimarket.nodes.node_ticket_classify_compute.models.model_ticket_for_classification import (
    ModelTicketForClassification,
)


@dataclass(frozen=True)
class SeedCase:
    """One classifier seed-case: input ticket + expected classification."""

    label: str
    ticket: ModelTicketForClassification
    expected_buildability: EnumBuildability
    expected_seam_source: str
    criterion: str


# --- Three seed archetypes (per DoD) -----------------------------------------
#
# 1. Known-good buildable    — contract seams with all mocks available
# 2. Known-bad unbuildable   — contract seams with an unmockable dependency
# 3. Edge-case ambiguous     — no seam boundaries, keyword heuristics decide

SEED_KNOWN_GOOD_BUILDABLE = SeedCase(
    label="known_good_buildable",
    ticket=ModelTicketForClassification(
        ticket_id="SEED-001",
        title="Implement handler_ticket_work with mockable bus",
        description="Wire the handler against ProtocolEventBus (mock available).",
        labels=("build-loop-seed",),
        seam_boundaries=ModelSeamBoundaries(
            consumes=(
                ModelConsumedProtocol(
                    protocol="ProtocolEventBus",
                    module="omnibase_spi.protocols.protocol_event_bus",
                    mock_available=True,
                ),
            ),
        ),
    ),
    expected_buildability=EnumBuildability.AUTO_BUILDABLE,
    expected_seam_source="contract",
    criterion="contract_all_mockable",
)

SEED_KNOWN_BAD_UNBUILDABLE = SeedCase(
    label="known_bad_unbuildable",
    ticket=ModelTicketForClassification(
        ticket_id="SEED-002",
        title="Integrate with not-yet-shipped downstream service",
        description="Requires ProtocolFutureInfra which does not yet exist.",
        labels=("build-loop-seed",),
        seam_boundaries=ModelSeamBoundaries(
            consumes=(
                ModelConsumedProtocol(
                    protocol="ProtocolFutureInfra",
                    module="omnibase_spi.protocols.protocol_future_infra",
                    mock_available=False,
                ),
            ),
        ),
    ),
    expected_buildability=EnumBuildability.BLOCKED,
    expected_seam_source="contract",
    criterion="contract_unmockable",
)

SEED_EDGE_AMBIGUOUS = SeedCase(
    label="edge_case_ambiguous",
    ticket=ModelTicketForClassification(
        ticket_id="SEED-003",
        title="Evaluate tradeoff for new auth design",
        description="Needs an RFC before implementation can proceed.",
        labels=("build-loop-seed",),
    ),
    expected_buildability=EnumBuildability.NEEDS_ARCH_DECISION,
    expected_seam_source="keyword_fallback",
    criterion="keyword_needs_arch_decision",
)

SEED_ARCHETYPES: tuple[SeedCase, ...] = (
    SEED_KNOWN_GOOD_BUILDABLE,
    SEED_KNOWN_BAD_UNBUILDABLE,
    SEED_EDGE_AMBIGUOUS,
)


# --- Criterion coverage fixtures ---------------------------------------------
#
# Each of the classifier's five buildability criteria gets a fixture:
#   1. contract_empty_consumes  → AUTO_BUILDABLE via contract, 0.7 confidence
#   2. contract_all_mockable    → AUTO_BUILDABLE via contract, 0.9 confidence
#   3. contract_unmockable      → BLOCKED via contract
#   4. keyword_skip_state       → SKIP via keyword fallback (terminal state)
#   5. keyword_auto_buildable   → AUTO_BUILDABLE via keyword fallback default

SEED_CRITERION_CONTRACT_EMPTY = SeedCase(
    label="contract_empty_consumes",
    ticket=ModelTicketForClassification(
        ticket_id="SEED-101",
        title="Refactor pure-internal reducer",
        description="No external protocols consumed.",
        labels=("build-loop-seed",),
        seam_boundaries=ModelSeamBoundaries(),
    ),
    expected_buildability=EnumBuildability.AUTO_BUILDABLE,
    expected_seam_source="contract",
    criterion="contract_empty_consumes",
)

SEED_CRITERION_CONTRACT_MOCKABLE = SEED_KNOWN_GOOD_BUILDABLE
SEED_CRITERION_CONTRACT_UNMOCKABLE = SEED_KNOWN_BAD_UNBUILDABLE

SEED_CRITERION_KEYWORD_SKIP = SeedCase(
    label="keyword_skip_state",
    ticket=ModelTicketForClassification(
        ticket_id="SEED-104",
        title="Old ticket left over from last cycle",
        state="Done",
        labels=("build-loop-seed",),
    ),
    expected_buildability=EnumBuildability.SKIP,
    expected_seam_source="keyword_fallback",
    criterion="keyword_skip",
)

SEED_CRITERION_KEYWORD_AUTO = SeedCase(
    label="keyword_auto_buildable",
    ticket=ModelTicketForClassification(
        ticket_id="SEED-105",
        title="Add new compute node and wire its handler",
        description="Standard implement+wire body copy.",
        labels=("build-loop-seed",),
    ),
    expected_buildability=EnumBuildability.AUTO_BUILDABLE,
    expected_seam_source="keyword_fallback",
    criterion="keyword_auto_buildable",
)

SEED_CRITERIA_COVERAGE: tuple[SeedCase, ...] = (
    SEED_CRITERION_CONTRACT_EMPTY,
    SEED_CRITERION_CONTRACT_MOCKABLE,
    SEED_CRITERION_CONTRACT_UNMOCKABLE,
    SEED_CRITERION_KEYWORD_SKIP,
    SEED_CRITERION_KEYWORD_AUTO,
)


__all__: list[str] = [
    "SEED_ARCHETYPES",
    "SEED_CRITERIA_COVERAGE",
    "SEED_CRITERION_CONTRACT_EMPTY",
    "SEED_CRITERION_CONTRACT_MOCKABLE",
    "SEED_CRITERION_CONTRACT_UNMOCKABLE",
    "SEED_CRITERION_KEYWORD_AUTO",
    "SEED_CRITERION_KEYWORD_SKIP",
    "SEED_EDGE_AMBIGUOUS",
    "SEED_KNOWN_BAD_UNBUILDABLE",
    "SEED_KNOWN_GOOD_BUILDABLE",
    "SeedCase",
]
