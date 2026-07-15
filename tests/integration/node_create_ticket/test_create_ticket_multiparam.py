# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Multi-parameter integration proof for node_create_ticket (OMN-13679, WS-5).

Variant A (COMPUTE, direct in-process handler call). ``HandlerCreateTicket``
validates parent/blocked-by IDs, detects seam signals, synthesizes a
structured description, and — since OMN-14547 — calls the injected Linear
client to create the ticket. Real network I/O is never exercised here; a
mock ``LinearTicketClientProtocol`` implementation stands in for the Linear
GraphQL API so the test drives the handler directly and asserts the typed
``ModelCreateTicketResult`` fields, including a non-empty ``ticket_id`` on
every "created" case.

Each parametrized case exercises a distinct mode/flag combination:
- plain title (no seam) → stub completeness
- seam titles (topics / database) → seam detection + full completeness
- dry-run short-circuit (no Linear call)
- NEGATIVE CONTROL: malformed parent / blocked-by ID → validation_errors finding (no Linear call)
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_create_ticket.handlers.handler_create_ticket import (
    HandlerCreateTicket,
    ModelCreateTicketRequest,
    ModelCreateTicketResult,
)


class _MockLinearTicketClient:
    """Minimal injectable Linear client — never touches the network."""

    def __init__(
        self,
        created_id: str = "OMN-90002",
        created_url: str = "https://linear.app/omninode/issue/OMN-90002",
    ) -> None:
        self._created_id = created_id
        self._created_url = created_url
        self.create_ticket_calls: list[dict[str, object]] = []

    def create_ticket(
        self, *, title: str, description: str, team: str, parent: str | None
    ) -> tuple[str, str]:
        self.create_ticket_calls.append(
            {"title": title, "description": description, "team": team, "parent": parent}
        )
        return self._created_id, self._created_url


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
    # Injected on every case; only the "created" cases actually invoke it —
    # dry_run and error cases short-circuit before the Linear call.
    handler = HandlerCreateTicket(linear_client=_MockLinearTicketClient())
    result = handler.handle(ModelCreateTicketRequest(**payload))

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

    if expected["status"] == "created":
        # OMN-14547: a "created" result must always carry a real ticket_id.
        assert result.ticket_id, "created result must have a non-empty ticket_id"


@pytest.mark.integration
def test_minimal_skill_cli_payload_shape_validates() -> None:
    """Regression for OMN-13964.

    The ``onex skill create_ticket`` CLI path resolves ``skill_mapping.yaml``,
    which declares ``allow-arch-violation`` with ``default: false`` — so the
    runtime injects ``allow_arch_violation: False`` into *every* payload, even a
    minimal ``--title X --team Y`` invocation. ``ModelCreateTicketRequest`` is
    ``extra="forbid"``, so before this field existed the exact payload shape the
    CLI produces failed with ``extra_forbidden`` on 100% of invocations (the
    WS-D/D2 dogfood repro). This asserts that injected shape constructs and the
    handler processes it end-to-end.

    Updated for OMN-14547: a "created" result now carries a real (mocked)
    ticket_id/ticket_url instead of the fake-success empty strings.
    """
    injected_payload: dict[str, object] = {
        "title": "Dogfood the create_ticket rail",
        "team": "Omninode",
        "allow_arch_violation": False,  # what the CLI force-injects from the mapping default
    }

    request = ModelCreateTicketRequest(**injected_payload)
    assert request.allow_arch_violation is False

    result = HandlerCreateTicket(linear_client=_MockLinearTicketClient()).handle(
        request
    )
    assert result.status == "created"
    assert result.ticket_id == "OMN-90002"
    assert result.ticket_url == "https://linear.app/omninode/issue/OMN-90002"
    assert result.validation_errors == []


@pytest.mark.integration
@pytest.mark.parametrize("allow", [False, True])
def test_allow_arch_violation_field_is_accepted(allow: bool) -> None:
    """The contract-declared ``allow_arch_violation`` input is a typed field."""
    request = ModelCreateTicketRequest(
        title="Ship with an arch override", allow_arch_violation=allow
    )
    assert request.allow_arch_violation is allow
    result = HandlerCreateTicket(linear_client=_MockLinearTicketClient()).handle(
        request
    )
    assert result.status == "created"
    assert result.ticket_id


@pytest.mark.integration
def test_created_ticket_emits_structured_description_body() -> None:
    """A real (non-dry-run, valid) request must materialize a DoD checklist."""
    result = HandlerCreateTicket(linear_client=_MockLinearTicketClient()).handle(
        ModelCreateTicketRequest(
            title="Ship the widget",
            description="implement the widget",
            repo="omnimarket",
        )
    )
    assert result.status == "created"
    assert "## Definition of Done" in result.description_body
    assert "- [ ] Verified in `omnimarket`" in result.description_body
