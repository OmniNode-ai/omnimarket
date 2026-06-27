# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Multi-parameter integration proof for node_create_ticket (OMN-13679, WS-5).

Variant A (COMPUTE, direct in-process handler call). ``HandlerCreateTicket`` is
pure logic — it validates parent/blocked-by IDs, detects seam signals, and
synthesizes a structured description. No Linear client is touched at this node
boundary (creation is performed downstream by the platform), so the test drives
the handler directly and asserts the typed ``ModelCreateTicketResult`` fields.

Each parametrized case exercises a distinct mode/flag combination:
- plain title (no seam) → stub completeness
- seam titles (topics / database) → seam detection + full completeness
- dry-run short-circuit
- NEGATIVE CONTROL: malformed parent / blocked-by ID → validation_errors finding
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_create_ticket.handlers.handler_create_ticket import (
    HandlerCreateTicket,
    ModelCreateTicketRequest,
    ModelCreateTicketResult,
)

# Each case: (payload kwargs, expected field assertions).
_CASES = [
    pytest.param(
        {"title": "Refactor the cost projection helper", "description": "tidy code"},
        {
            "status": "created",
            "is_seam_ticket": False,
            "interfaces_touched": [],
            "contract_completeness": "stub",
        },
        id="plain-non-seam-stub",
    ),
    pytest.param(
        {
            "title": "Wire a new Kafka topic consumer",
            "description": "publish to redpanda event bus",
        },
        {
            "status": "created",
            "is_seam_ticket": True,
            "interfaces_touched": ["topics"],
            "contract_completeness": "full",
        },
        id="seam-topics-full",
    ),
    pytest.param(
        {
            "title": "Add a postgres migration",
            "description": "new schema table for the analytics database",
        },
        {
            "status": "created",
            "is_seam_ticket": True,
            "interfaces_touched": ["database"],
            "contract_completeness": "full",
        },
        id="seam-database-full",
    ),
    pytest.param(
        {"title": "Dry run me", "dry_run": True},
        {
            "status": "dry_run",
            "dry_run": True,
            "is_seam_ticket": False,
        },
        id="dry-run-short-circuit",
    ),
    pytest.param(
        {"title": "Child work", "parent": "FOO-123"},
        {
            "status": "error",
            "has_validation_error_substr": "Invalid parent ID format",
        },
        id="negative-bad-parent-id",
    ),
    pytest.param(
        {"title": "Blocked work", "blocked_by": ["OMN-1", "not-a-ticket"]},
        {
            "status": "error",
            "has_validation_error_substr": "Invalid blocked_by ID format",
        },
        id="negative-bad-blocked-by-id",
    ),
]


@pytest.mark.integration
@pytest.mark.parametrize(("payload", "expected"), _CASES)
def test_create_ticket_multiparam(
    payload: dict[str, object], expected: dict[str, object]
) -> None:
    result = HandlerCreateTicket().handle(ModelCreateTicketRequest(**payload))

    assert isinstance(result, ModelCreateTicketResult)
    assert result.status == expected["status"]

    if "is_seam_ticket" in expected:
        assert result.is_seam_ticket == expected["is_seam_ticket"]
    if "interfaces_touched" in expected:
        assert result.interfaces_touched == expected["interfaces_touched"]
    if "contract_completeness" in expected:
        assert result.contract_completeness == expected["contract_completeness"]
    if "dry_run" in expected:
        assert result.dry_run == expected["dry_run"]

    if "has_validation_error_substr" in expected:
        # NEGATIVE CONTROL: a known-bad fixture must yield a structured finding.
        assert result.validation_errors, "expected at least one validation error"
        assert any(
            str(expected["has_validation_error_substr"]) in err
            for err in result.validation_errors
        ), result.validation_errors
    else:
        assert result.validation_errors == []


@pytest.mark.integration
def test_created_ticket_emits_structured_description_body() -> None:
    """A real (non-dry-run, valid) request must materialize a DoD checklist."""
    result = HandlerCreateTicket().handle(
        ModelCreateTicketRequest(
            title="Ship the widget",
            description="implement the widget",
            repo="omnimarket",
        )
    )
    assert result.status == "created"
    assert "## Definition of Done" in result.description_body
    assert "- [ ] Verified in `omnimarket`" in result.description_body
