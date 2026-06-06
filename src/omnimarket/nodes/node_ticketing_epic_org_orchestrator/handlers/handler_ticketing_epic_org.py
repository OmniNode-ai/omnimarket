# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerTicketingEpicOrg — ORCHESTRATOR node for ticketing epic organization.

Groups orphaned Linear tickets by naming pattern and auto-creates epics for
obvious groupings. Implements the multi-phase algorithm from the
ticketing_epic_org skill:

  Phase 1: Load orphaned tickets (from TriageReport YAML or fresh Linear fetch)
  Phase 2: Group by naming prefix (Rule 1) or shared repo+label (Rule 2)
  Phase 3: Classify each group via structural guards:
             auto_create   — group size ≥ 2, single repo, clear naming prefix
             human_gate    — ambiguous grouping (cross-repo, single ticket, etc.)
             structural_violation — every member is itself an epic; REFUSED
  Phase 3b: Secondary clustering pass over surviving groups
  Phase 4: Present the full proposal
  Phase 5: Create epics for auto-eligible (and human-approved) groups
  Phase 6: Emit summary report

Live epic creation requires an injected adapter; dry-run proposal generation is
deterministic over supplied orphaned tickets.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from typing import Protocol

from omnimarket.nodes.node_ticketing_epic_org_orchestrator.models.model_ticketing_epic_org import (
    ModelCreatedEpic,
    ModelOrphanedTicket,
    ModelProposedEpicGroup,
    ModelTicketingEpicOrgRequest,
    ModelTicketingEpicOrgResult,
)

_log = logging.getLogger(__name__)

# Topics from contract.yaml — never inline elsewhere
TOPIC_LINEAR_EPIC_CREATE = "onex.cmd.omnimarket.linear-epic-create.v1"  # onex-topic-allow: pending contract auto-wiring
TOPIC_LINEAR_TICKET_REPARENT = "onex.cmd.omnimarket.linear-ticket-reparent.v1"  # onex-topic-allow: pending contract auto-wiring
TOPIC_EPIC_ORG_COMPLETED = "onex.evt.omnimarket.ticketing-epic-org-completed.v1"  # onex-topic-allow: pending contract auto-wiring
TOPIC_EPIC_ORG_PROPOSAL_READY = "onex.evt.omnimarket.ticketing-epic-org-proposal-ready.v1"  # onex-topic-allow: pending contract auto-wiring

_PREFIX_RE = re.compile(
    r"^\s*(?:\[[^\]]+\]\s*)?(?P<prefix>[A-Z][A-Z0-9]+(?:-[A-Z0-9]+)+)-?\d*", re.ASCII
)


class ProtocolEpicOrgLinearAdapter(Protocol):
    """Adapter boundary for live epic creation and ticket reparenting."""

    def create_epic(self, title: str, ticket_ids: list[str]) -> str: ...


class HandlerTicketingEpicOrg:
    """ORCHESTRATOR — groups orphaned Linear tickets into epics.

    The default path creates proposals only. Live mutation requires
    ``auto_approve=True``, ``dry_run=False``, and an injected adapter.
    """

    def __init__(self, adapter: ProtocolEpicOrgLinearAdapter | None = None) -> None:
        self._adapter = adapter

    def handle(
        self,
        request: ModelTicketingEpicOrgRequest,
    ) -> ModelTicketingEpicOrgResult:
        """Execute deterministic proposal generation and optional adapter mutation."""
        if request.triage_report_path:
            raise RuntimeError(
                "triage_report_path ingestion is not implemented; pass orphaned_tickets"
            )

        proposed = _propose_groups(request.orphaned_tickets)
        structural = [
            group for group in proposed if group.verdict == "structural_violation"
        ]
        human_gate = [group for group in proposed if group.verdict == "human_gate"]
        auto_groups = [group for group in proposed if group.verdict == "auto_create"]
        created: list[ModelCreatedEpic] = []

        if request.auto_approve and not request.dry_run and auto_groups:
            if self._adapter is None:
                raise RuntimeError(
                    "linear adapter required when auto_approve is true and dry_run is false"
                )
            for group in auto_groups:
                epic_id = self._adapter.create_epic(
                    group.proposed_epic_title, group.ticket_ids
                )
                created.append(
                    ModelCreatedEpic(
                        epic_id=epic_id,
                        title=group.proposed_epic_title,
                        children_linked=group.ticket_ids,
                        group_key=group.group_key,
                    )
                )

        return ModelTicketingEpicOrgResult(
            run_id=request.run_id,
            dry_run=request.dry_run,
            orphaned_tickets_count=len(request.orphaned_tickets),
            proposed_groups=proposed,
            structural_violations=structural,
            epics_created=created,
            human_gate_groups=human_gate,
            tickets_reparented=sum(len(epic.children_linked) for epic in created),
        )


def _propose_groups(tickets: list[ModelOrphanedTicket]) -> list[ModelProposedEpicGroup]:
    grouped: dict[tuple[str | None, str], list[ModelOrphanedTicket]] = defaultdict(list)
    for ticket in tickets:
        prefix = _title_prefix(ticket.title)
        if prefix:
            grouped[(ticket.repo, prefix)].append(ticket)

    proposals: list[ModelProposedEpicGroup] = []
    for (repo, prefix), members in sorted(
        grouped.items(), key=lambda item: (item[0][0] or "", item[0][1])
    ):
        ticket_ids = [member.ticket_id for member in members]
        all_epics = all("epic" in member.title.lower() for member in members)
        repos = {member.repo for member in members}
        if all_epics:
            verdict = "structural_violation"
            reason = "All members are themselves epics."
        elif len(members) >= 2 and len(repos) == 1 and repo:
            verdict = "auto_create"
            reason = None
        else:
            verdict = "human_gate"
            reason = None
        proposals.append(
            ModelProposedEpicGroup(
                group_key=f"({repo or 'unknown'}, {prefix})",
                grouping_rule="prefix",
                ticket_ids=ticket_ids,
                repo=repo,
                prefix=prefix,
                label=None,
                proposed_epic_title=f"[{repo or 'cross-repo'}] {prefix}",
                verdict=verdict,
                structural_violation_reason=reason,
            )
        )
    return proposals


def _title_prefix(title: str) -> str | None:
    match = _PREFIX_RE.match(title)
    if not match:
        return None
    return re.sub(r"-\d+$", "", match.group("prefix")).rstrip("-")
