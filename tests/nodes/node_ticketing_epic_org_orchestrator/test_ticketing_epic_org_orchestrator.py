# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for node_ticketing_epic_org_orchestrator [OMN-12202]."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_ticketing_epic_org_orchestrator import (
    HandlerTicketingEpicOrg,
    ModelCreatedEpic,
    ModelOrphanedTicket,
    ModelProposedEpicGroup,
    ModelTicketingEpicOrgRequest,
    ModelTicketingEpicOrgResult,
)

# ---------------------------------------------------------------------------
# Import / public surface
# ---------------------------------------------------------------------------


class TestPublicSurface:
    @pytest.mark.unit
    def test_all_symbols_importable(self) -> None:
        assert HandlerTicketingEpicOrg is not None
        assert ModelOrphanedTicket is not None
        assert ModelProposedEpicGroup is not None
        assert ModelCreatedEpic is not None
        assert ModelTicketingEpicOrgRequest is not None
        assert ModelTicketingEpicOrgResult is not None


# ---------------------------------------------------------------------------
# ModelOrphanedTicket validation
# ---------------------------------------------------------------------------


class TestModelOrphanedTicket:
    @pytest.mark.unit
    def test_minimal_ticket(self) -> None:
        t = ModelOrphanedTicket(ticket_id="OMN-1234", title="Do something useful")
        assert t.ticket_id == "OMN-1234"
        assert t.repo is None
        assert t.labels == []
        assert t.state == ""
        assert t.priority is None

    @pytest.mark.unit
    def test_full_ticket(self) -> None:
        t = ModelOrphanedTicket(
            ticket_id="OMN-5678",
            title="[omniclaude] DB-SPLIT-03: FK scan",
            repo="omniclaude",
            labels=["omniclaude", "database"],
            state="In Progress",
            priority=2,
        )
        assert t.repo == "omniclaude"
        assert t.priority == 2
        assert "omniclaude" in t.labels

    @pytest.mark.unit
    def test_frozen(self) -> None:
        t = ModelOrphanedTicket(ticket_id="OMN-1", title="X")
        with pytest.raises(ValidationError):
            t.ticket_id = "OMN-2"  # type: ignore[misc]

    @pytest.mark.unit
    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            ModelOrphanedTicket(
                ticket_id="OMN-1",
                title="X",
                unexpected="boom",  # type: ignore[call-arg]
            )


# ---------------------------------------------------------------------------
# ModelProposedEpicGroup validation
# ---------------------------------------------------------------------------


class TestModelProposedEpicGroup:
    def _make_auto_create(self) -> ModelProposedEpicGroup:
        return ModelProposedEpicGroup(
            group_key="(omniclaude, DB-SPLIT)",
            grouping_rule="prefix",
            ticket_ids=["OMN-2068", "OMN-2069", "OMN-2070"],
            repo="omniclaude",
            prefix="DB-SPLIT",
            label=None,
            proposed_epic_title="[omniclaude] DB-SPLIT — Database Split",
            verdict="auto_create",
            structural_violation_reason=None,
        )

    @pytest.mark.unit
    def test_auto_create_group(self) -> None:
        g = self._make_auto_create()
        assert g.verdict == "auto_create"
        assert g.grouping_rule == "prefix"
        assert len(g.ticket_ids) == 3

    @pytest.mark.unit
    def test_structural_violation_group(self) -> None:
        g = ModelProposedEpicGroup(
            group_key="(all-epics, EPIC)",
            grouping_rule="prefix",
            ticket_ids=["OMN-100", "OMN-200"],
            repo=None,
            prefix=None,
            label=None,
            proposed_epic_title="[Meta] All Epics",
            verdict="structural_violation",
            structural_violation_reason="All members are themselves epics.",
        )
        assert g.verdict == "structural_violation"
        assert g.structural_violation_reason is not None

    @pytest.mark.unit
    def test_human_gate_group(self) -> None:
        g = ModelProposedEpicGroup(
            group_key="(cross-repo, SEAM)",
            grouping_rule="secondary_cluster",
            ticket_ids=["OMN-300"],
            repo=None,
            prefix="SEAM",
            label=None,
            proposed_epic_title="SEAM — Cross-Repo Seam Work",
            verdict="human_gate",
            structural_violation_reason=None,
        )
        assert g.verdict == "human_gate"

    @pytest.mark.unit
    def test_invalid_grouping_rule(self) -> None:
        with pytest.raises(ValidationError):
            ModelProposedEpicGroup(
                group_key="x",
                grouping_rule="unknown_rule",  # invalid
                ticket_ids=[],
                repo=None,
                prefix=None,
                label=None,
                proposed_epic_title="X",
                verdict="auto_create",
                structural_violation_reason=None,
            )

    @pytest.mark.unit
    def test_invalid_verdict(self) -> None:
        with pytest.raises(ValidationError):
            ModelProposedEpicGroup(
                group_key="x",
                grouping_rule="prefix",
                ticket_ids=[],
                repo=None,
                prefix=None,
                label=None,
                proposed_epic_title="X",
                verdict="maybe_create",  # invalid
                structural_violation_reason=None,
            )

    @pytest.mark.unit
    def test_frozen(self) -> None:
        g = self._make_auto_create()
        with pytest.raises(ValidationError):
            g.verdict = "human_gate"  # type: ignore[misc]

    @pytest.mark.unit
    def test_serialization_round_trip(self) -> None:
        g = self._make_auto_create()
        restored = ModelProposedEpicGroup.model_validate(g.model_dump())
        assert restored == g


# ---------------------------------------------------------------------------
# ModelTicketingEpicOrgRequest validation
# ---------------------------------------------------------------------------


class TestModelTicketingEpicOrgRequest:
    @pytest.mark.unit
    def test_defaults(self) -> None:
        req = ModelTicketingEpicOrgRequest()
        assert req.triage_report_path is None
        assert req.orphaned_tickets == []
        assert req.dry_run is False
        assert req.auto_approve is False
        assert req.run_id == ""

    @pytest.mark.unit
    def test_with_dry_run(self) -> None:
        req = ModelTicketingEpicOrgRequest(dry_run=True)
        assert req.dry_run is True

    @pytest.mark.unit
    def test_with_orphaned_tickets(self) -> None:
        tickets = [
            ModelOrphanedTicket(ticket_id="OMN-1", title="Ticket one"),
            ModelOrphanedTicket(ticket_id="OMN-2", title="Ticket two"),
        ]
        req = ModelTicketingEpicOrgRequest(orphaned_tickets=tickets)
        assert len(req.orphaned_tickets) == 2

    @pytest.mark.unit
    def test_frozen(self) -> None:
        req = ModelTicketingEpicOrgRequest()
        with pytest.raises(ValidationError):
            req.dry_run = True  # type: ignore[misc]

    @pytest.mark.unit
    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            ModelTicketingEpicOrgRequest(unknown="bad")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# ModelTicketingEpicOrgResult validation
# ---------------------------------------------------------------------------


class TestModelTicketingEpicOrgResult:
    @pytest.mark.unit
    def test_empty_result(self) -> None:
        r = ModelTicketingEpicOrgResult(orphaned_tickets_count=0)
        assert r.orphaned_tickets_count == 0
        assert r.epics_created == []
        assert r.proposed_groups == []
        assert r.structural_violations == []
        assert r.human_gate_groups == []
        assert r.tickets_reparented == 0
        assert r.dry_run is False

    @pytest.mark.unit
    def test_with_created_epic(self) -> None:
        epic = ModelCreatedEpic(
            epic_id="OMN-9999",
            title="[omniclaude] DB-SPLIT — Database Split",
            children_linked=["OMN-2068", "OMN-2069"],
            group_key="(omniclaude, DB-SPLIT)",
        )
        r = ModelTicketingEpicOrgResult(
            orphaned_tickets_count=2,
            epics_created=[epic],
            tickets_reparented=2,
        )
        assert len(r.epics_created) == 1
        assert r.tickets_reparented == 2

    @pytest.mark.unit
    def test_frozen(self) -> None:
        r = ModelTicketingEpicOrgResult(orphaned_tickets_count=0)
        with pytest.raises(ValidationError):
            r.tickets_reparented = 5  # type: ignore[misc]

    @pytest.mark.unit
    def test_orphaned_tickets_count_non_negative(self) -> None:
        with pytest.raises(ValidationError):
            ModelTicketingEpicOrgResult(orphaned_tickets_count=-1)


# ---------------------------------------------------------------------------
# Handler behavior
# ---------------------------------------------------------------------------


class TestHandlerTicketingEpicOrgBehavior:
    @pytest.mark.unit
    def test_handle_dry_run_groups_prefixes(self) -> None:
        handler = HandlerTicketingEpicOrg()
        req = ModelTicketingEpicOrgRequest(
            dry_run=True,
            orphaned_tickets=[
                ModelOrphanedTicket(
                    ticket_id="OMN-2068",
                    title="[omnimarket] DB-SPLIT-01: FK scan",
                    repo="omnimarket",
                ),
                ModelOrphanedTicket(
                    ticket_id="OMN-2069",
                    title="[omnimarket] DB-SPLIT-02: migrate rows",
                    repo="omnimarket",
                ),
            ],
        )

        result = handler.handle(req)

        assert result.dry_run is True
        assert result.orphaned_tickets_count == 2
        assert len(result.proposed_groups) == 1
        assert result.proposed_groups[0].verdict == "auto_create"
        assert result.epics_created == []

    @pytest.mark.unit
    def test_handler_instantiates_without_args(self) -> None:
        handler = HandlerTicketingEpicOrg()
        assert handler is not None

    @pytest.mark.unit
    def test_live_auto_approve_requires_adapter(self) -> None:
        handler = HandlerTicketingEpicOrg()
        req = ModelTicketingEpicOrgRequest(
            auto_approve=True,
            orphaned_tickets=[
                ModelOrphanedTicket(
                    ticket_id="OMN-2068",
                    title="[omnimarket] DB-SPLIT-01: FK scan",
                    repo="omnimarket",
                ),
                ModelOrphanedTicket(
                    ticket_id="OMN-2069",
                    title="[omnimarket] DB-SPLIT-02: migrate rows",
                    repo="omnimarket",
                ),
            ],
        )
        with pytest.raises(RuntimeError, match="linear adapter required"):
            handler.handle(req)
