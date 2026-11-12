# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one goal-scope admission rule (OMN-23851), ported from the onex-overlays plugin with its reads removed.

The caller gathers everything: the candidates with their titles and bodies, the rule
(OMN-22570) and the ticket scope (OMN-23184) from their overlays, the ticket facts from the
local Linear mirror or one live read, and the tickets a read failed on with why. The handler
walks those facts and decides; it reads no file, runs no process, has no clock and no env. The
walk itself is the plugin's own ``declared_ticket`` and ``ticket_scope_verdict``, unchanged in
behaviour: parents alone first (OMN-23310), a root on the chain deciding without a project read,
projects read only on a chain with no root, lowest first, a read that failed never undoing a yes
already read, and an unread ticket never admitted.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection, Mapping
from typing import Any, Literal

from omnimarket.models.goal_scope_admission import (
    ModelGoalAdmission,
    ModelGoalScopeAdmissionRequest,
    ModelGoalScopeAdmissionResult,
    ModelTicketFacts,
    ModelTicketScope,
)

GOAL_TICKET = re.compile(r"\bOMN-\d+\b")
GOAL_BODY_TICKET = re.compile(r"^[ \t]*Ticket:[ \t]*(OMN-\d+)\b", re.MULTILINE)


class _TicketUnreadError(Exception):
    """A ticket whose asked-for facts were not supplied: unread, never admitted, never a no."""


def declared_ticket(facts: Mapping[str, Any]) -> str:
    """The ticket a pull request declares: the first ticket its title names (the ``type(OMN-n): ...`` form every lane uses), else the
    first ``Ticket: OMN-n`` line of its body, else ''. One ticket per pull request: a body that also names an umbrella does not
    make the pull request a member of it."""
    if match := GOAL_TICKET.search(str(facts.get("title") or "")):
        return match.group(0)
    match = GOAL_BODY_TICKET.search(str(facts.get("body") or ""))
    return match.group(1) if match else ""


def ticket_scope_verdict(
    ticket: str,
    roots: Collection[str],
    scope: ModelTicketScope | None,
    max_depth: int,
    facts_of: Callable[..., ModelTicketFacts],
) -> tuple[Literal["ticket", "project"], str] | None:
    """The one scope reader (OMN-23184): ``("ticket", chain)`` when ``ticket`` or an ancestor (to ``max_depth`` hops up) is one of ``roots``
    (``chain`` is ``OMN-9<OMN-4<OMN-22478``, the ticket up to the root), ``("project", name)`` when no root is on the chain and the first
    ticket on the way up that sits in a project ``scope`` lists is ``ticket`` itself (``name`` is the project) or an ancestor
    (``name@ancestor``), else None, a chain that ends below no root and in no listed project. ``facts_of(ticket, parent=, project=)``
    returns a ticket's facts with the asked fields known, and raises ``_TicketUnreadError`` for a ticket it cannot read.

    The chain is walked on parents alone first (OMN-23310: the old parent reader kept only parents, the old project reader only projects,
    and a fact bridged from either is partial); a root on it decides without a project read. Only a chain with no root reads the projects
    of its tickets, lowest first. A read that failed does not undo a yes already read (a listed project on a ticket read, or a root
    reached), and with no yes it is the caller's to hold on: an unread ticket is never a yes."""
    chain = [ticket]
    failure: _TicketUnreadError | None = None
    for hop in range(max_depth + 1):
        node = chain[-1]
        if node in roots:
            return "ticket", "<".join(chain)
        if hop == max_depth:
            break
        try:
            parent = facts_of(node, parent=True, project=False).parent
        except _TicketUnreadError as exc:
            failure = exc
            break
        if parent is None or parent in chain:
            break
        chain.append(parent)
    if scope is not None:
        for node in chain:
            try:
                facts = facts_of(node, parent=False, project=True)
            except _TicketUnreadError as exc:
                failure = failure or exc
                continue
            if scope.lists(facts.project_id, facts.project_name):
                name = facts.project_name or facts.project_id
                return "project", name if node == ticket else f"{name}@{node}"
    if failure is not None:
        raise failure
    return None


class HandlerGoalScopeAdmission:
    """Admit candidate changes into the goal by the ticket each declares (OMN-23851)."""

    def handle(
        self, request: ModelGoalScopeAdmissionRequest
    ) -> ModelGoalScopeAdmissionResult:
        def facts_of(ticket: str, *, parent: bool, project: bool) -> ModelTicketFacts:
            """A ticket's facts with the asked fields known; a ticket the caller could not read raises."""
            if (reason := request.unread_tickets.get(ticket)) is not None:
                raise _TicketUnreadError(reason)
            facts = request.ticket_facts.get(ticket)
            if facts is not None and facts.covers(parent=parent, project=project):
                return facts
            raise _TicketUnreadError(f"no facts were supplied for {ticket}")

        admitted: dict[str, ModelGoalAdmission] = {}
        not_admitted: dict[str, str] = {}
        unread: dict[str, str] = {}
        for ref in sorted(request.candidates):
            candidate = request.candidates[ref]
            ticket = declared_ticket({"title": candidate.title, "body": candidate.body})
            if not ticket:
                not_admitted[ref] = "declares no ticket"
                continue
            if request.rule is None and request.scope is None:
                not_admitted[ref] = "no goal rule or ticket scope is installed"
                continue
            roots = request.rule.roots if request.rule is not None else frozenset()
            depth = request.rule.max_depth if request.rule is not None else 0
            try:
                verdict = ticket_scope_verdict(
                    ticket, roots, request.scope, depth, facts_of
                )
            except _TicketUnreadError as exc:
                unread[ref] = str(exc)[:160]
                continue
            if verdict is None:
                not_admitted[ref] = (
                    f"{ticket} is under no goal root and in no listed project"
                )
                continue
            admitted[ref] = ModelGoalAdmission(via=verdict[0], label=verdict[1])
        return ModelGoalScopeAdmissionResult(
            admitted=admitted, not_admitted=not_admitted, unread=unread
        )
