# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Turn the tier-1 source tickets into candidates from Linear and watcher facts (OMN-20668).

A ticket Linear did not return, one that is done or canceled, an epic and one already in an
open PR are counted and dropped; ownership, assignment, fences and holds are the selection's
to judge. A failed ticket or watcher read names every source that was read unreadable.
"""

import re
from collections import Counter

from ..models.model_lab_fill_sources import (
    ModelLabFillSourceAttachment,
    ModelLabFillSourceCandidate,
    ModelLabFillSourceCandidatesRequest,
    ModelLabFillSourceCandidatesResult,
    ModelLabFillSourceDiagnostic,
    ModelLabFillSourceOpenPr,
)
from .handler_lab_fill_source_tickets import TICKET_RE, source_error

PULL_RE = re.compile("github[.]com/[^/]+/([^/]+)/pull/([0-9]+)")
TITLE_LIMIT = 120


def attached_prs(attachments: tuple[ModelLabFillSourceAttachment, ...]) -> list[str]:
    """The PRs a ticket's attachments link, newest attachment first."""
    found = []
    for attachment in attachments:
        match = PULL_RE.search(attachment.url)
        if match:
            found.append((attachment.created_at, match[1] + "#" + match[2]))
    return [pr for _, pr in sorted(found, reverse=True)]


def open_pr_tickets(prs: tuple[ModelLabFillSourceOpenPr, ...]) -> dict[str, list[str]]:
    """Tickets an open PR title names; change-control evidence companions do not count."""
    named: dict[str, list[str]] = {}
    for pr in prs:
        if pr.state != "OPEN" or (
            pr.repo == "onex_change_control"
            and pr.title.lower().startswith("evidence(")
        ):
            continue
        for ticket in TICKET_RE.findall(pr.title):
            named.setdefault(ticket, []).append(pr.key)
    return named


class HandlerLabFillSourceCandidates:
    """Drop what is done, an epic or in flight; keep the rest as tier-1 candidates."""

    def handle(
        self, request: ModelLabFillSourceCandidatesRequest
    ) -> ModelLabFillSourceCandidatesResult:
        sources = request.sources
        reads = {kind: d.read for kind, d in sources.sources.items()}
        drops = {kind: Counter(d.reasons) for kind, d in sources.sources.items()}
        candidates: list[ModelLabFillSourceCandidate] = []
        read_ok = [kind for kind, read in reads.items() if read == "ok"]
        if sources.wanted and request.read_error:
            for kind in read_ok:
                reads[kind] = "unreadable:" + source_error(request.read_error)
        elif sources.wanted:
            nodes = {t.identifier: t for t in request.tickets}
            in_flight = open_pr_tickets(request.open_prs)
            for kind in read_ok:
                evidence_of = {
                    f.ticket: f.evidence for f in sources.found if f.kind == kind
                }
                for ticket, evidence in evidence_of.items():
                    node = nodes.get(ticket)
                    if node is None:
                        drops[kind]["ticket-unreadable"] += 1
                    elif node.state_type in ("completed", "canceled"):
                        drops[kind]["done"] += 1
                    elif node.has_children:
                        drops[kind]["has-children"] += 1
                    elif ticket in in_flight:
                        drops[kind]["open-pr"] += 1
                    else:
                        candidates.append(
                            ModelLabFillSourceCandidate(
                                kind=kind,
                                ticket=ticket,
                                title=node.title[:TITLE_LIMIT],
                                labels=node.labels,
                                updated_at=node.updated_at,
                                project_id=node.project_id,
                                assignee_id=node.assignee_id,
                                ticket_prs=tuple(attached_prs(node.attachments)),
                                declared_repo=node.declared_repo,
                                evidence=evidence,
                            )
                        )
        return ModelLabFillSourceCandidatesResult(
            sources={
                kind: ModelLabFillSourceDiagnostic(
                    read=reads[kind],
                    dropped=sum(drops[kind].values()),
                    reasons=dict(drops[kind]),
                )
                for kind in reads
            },
            candidates=tuple(candidates),
        )
