# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Find or continue the one ticket of a dev-baseline cluster: search by the signature, never the title.

The dev-baseline reporter files every failing cluster again on every nightly run, so a report
is matched to the ticket it continues by the cluster's stable signature lines (the exact line,
or the legacy two-line form the old reporter filed). Fail closed, retry on the next run; the
occurrence is never lost, a report that ends unavailable is reported again whole. Each call of
the store runs under ``baseline_cluster.timeout_seconds``:

(a) ``search`` by the signature needles; a failed search or a search past the deadline ends the
    report ``unavailable`` and creates NOTHING.
(b) each found ticket is read oldest first; one that carries the signature is a carrying ticket.
    A read that fails or runs past the deadline ends the report ``unavailable``.
(c) the oldest carrying ticket that is open is the target: one comment with the new occurrence,
    no new ticket.
(d) else, the carrying tickets closed as duplicates are followed oldest first: each chain reads
    ``duplicate_of`` for at most ``max_duplicate_hops`` reads; a cycle, a hop limit or a dead
    link ends that chain with no survivor. A survivor that is open (it need NOT carry the
    signature) is commented on, and the report ends ``linked`` naming the closed duplicate as
    ``via``.
(e) else ``create`` once: the description the gate admits, under the overlay's parent, naming
    the oldest carrying closed ticket as ``previous_ticket`` when there is one. A refusal is a
    person's fix (``ticket-refused:<codes>``); a create that fails ends the report unavailable.
    A freshly created ticket gets no comment: its description already records the occurrence.

A comment that fails or runs past the deadline ends the report ``unavailable``: the next run
comments again on the same open ticket.
"""

from __future__ import annotations

from collections.abc import Callable

from omnimarket.nodes.node_baseline_cluster_ticket_effect.handlers.bounded_call import (
    CallDeadlineError,
    CallFailedError,
    bounded,
)
from omnimarket.nodes.node_baseline_cluster_ticket_effect.handlers.cluster_signature import (
    carries_signature,
    oldest_first,
    search_needles,
)
from omnimarket.nodes.node_baseline_cluster_ticket_effect.handlers.cluster_text import (
    occurrence_comment,
    ticket_description,
)
from omnimarket.nodes.node_baseline_cluster_ticket_effect.handlers.protocol_cluster_ticket_store import (
    ProtocolClusterTicketStore,
)
from omnimarket.nodes.node_baseline_cluster_ticket_effect.models.model_baseline_cluster_ticket import (
    ModelBaselineClusterReportRequest,
    ModelBaselineClusterReportResult,
)
from omnimarket.nodes.node_baseline_cluster_ticket_effect.models.model_cluster_ticket_store import (
    ModelClusterTicketCommentRequest,
    ModelClusterTicketCreateRequest,
    ModelClusterTicketLookupRequest,
    ModelClusterTicketLookupResult,
    ModelClusterTicketSearchRequest,
)

REFUSED_PREFIX = "ticket-refused:"
TITLE_TEMPLATE = "[dev-baseline] Test failure cluster: {cluster} ({repo})"
OPEN = "open"
DUPLICATE = "duplicate"
# The statuses a ticket cannot leave: every other found ticket is open (any non-terminal state).
CLOSED_STATUSES = ("done", "canceled", DUPLICATE)


class _EndOfReportError(Exception):
    """The report ends for this run with this result."""

    def __init__(self, result: ModelBaselineClusterReportResult) -> None:
        super().__init__(result.cause)
        self.result = result


def _unavailable(cause: str) -> _EndOfReportError:
    return _EndOfReportError(
        ModelBaselineClusterReportResult(state="unavailable", cause=cause)
    )


def _status_of(found: ModelClusterTicketLookupResult) -> str:
    """The ticket's status; a found ticket whose status the tracker omits is non-terminal."""
    return found.status if found.status is not None else OPEN


class HandlerBaselineClusterTicket:
    """Search by the cluster's signature lines, then comment, link or create; the store is injected."""

    def __init__(self, store: ProtocolClusterTicketStore) -> None:
        self._store = store

    def handle(
        self, request: ModelBaselineClusterReportRequest
    ) -> ModelBaselineClusterReportResult:
        try:
            carrying = self._search(request)
            target = self._open_target(carrying)
            if target is not None:
                self._comment(target, request)
                return ModelBaselineClusterReportResult(
                    state="commented", ticket=target
                )
            linked = self._follow_duplicate_chains(carrying, request)
            if linked is not None:
                survivor, via = linked
                self._comment(survivor, request)
                return ModelBaselineClusterReportResult(
                    state="linked", ticket=survivor, via=via
                )
            return self._create(carrying, request)
        except _EndOfReportError as ended:
            return ended.result

    def _call[Request, Result](
        self,
        operation: str,
        call: Callable[[Request], Result],
        argument: Request,
        request: ModelBaselineClusterReportRequest,
    ) -> Result:
        try:
            return bounded(call, argument, request.timeout_seconds)
        except CallDeadlineError:
            raise _unavailable(f"{operation}-timeout") from None
        except CallFailedError as failed:
            raise _unavailable(f"{operation}-failed:{failed.cause}") from None

    def _read(
        self, ticket: str, request: ModelBaselineClusterReportRequest
    ) -> ModelClusterTicketLookupResult:
        found = self._call(
            "read",
            self._store.read,
            ModelClusterTicketLookupRequest(ticket=ticket),
            request,
        )
        if found.state == "failed":
            raise _unavailable(f"read-failed:{found.cause}")
        return found

    def _search(
        self, request: ModelBaselineClusterReportRequest
    ) -> list[tuple[str, ModelClusterTicketLookupResult]]:
        """The found tickets that carry the signature, oldest first, with their reads."""
        searched = self._call(
            "search",
            self._store.search,
            ModelClusterTicketSearchRequest(
                needles=search_needles(request.repo, request.cluster)
            ),
            request,
        )
        if searched.state == "failed":
            raise _unavailable(f"search-failed:{searched.cause}")
        carrying: list[tuple[str, ModelClusterTicketLookupResult]] = []
        for ticket in oldest_first(searched.tickets):
            found = self._read(ticket, request)
            if found.state != "found":
                continue
            if not carries_signature(found.description, request.repo, request.cluster):
                continue
            carrying.append((ticket, found))
            if _status_of(found) == OPEN:
                # Oldest first: the first open carrying ticket is the target, and no later read can change that.
                break
        return carrying

    @staticmethod
    def _open_target(
        carrying: list[tuple[str, ModelClusterTicketLookupResult]],
    ) -> str | None:
        """The oldest carrying ticket that is open, the one the occurrence is commented on."""
        for ticket, found in carrying:
            if _status_of(found) == OPEN:
                return ticket
        return None

    def _follow_duplicate_chains(
        self,
        carrying: list[tuple[str, ModelClusterTicketLookupResult]],
        request: ModelBaselineClusterReportRequest,
    ) -> tuple[str, str] | None:
        """The first chain that ends on an open survivor, as (survivor, the closed duplicate)."""
        for ticket, found in carrying:
            if found.status != DUPLICATE or found.duplicate_of is None:
                continue
            survivor = self._follow_chain(ticket, found, request)
            if survivor is not None:
                return survivor, ticket
        return None

    def _follow_chain(
        self,
        start: str,
        start_found: ModelClusterTicketLookupResult,
        request: ModelBaselineClusterReportRequest,
    ) -> str | None:
        """The open survivor one duplicate chain ends on, or None when it has none.

        At most ``request.max_duplicate_hops`` reads; a cycle, a hop past the limit, a
        dead link or a survivor that is not open ends the chain with no survivor.
        """
        visited = {start}
        current = start_found
        for _ in range(request.max_duplicate_hops):
            nxt = current.duplicate_of
            if nxt is None or nxt in visited:
                return None
            visited.add(nxt)
            found = self._read(nxt, request)
            if found.state != "found":
                return None
            status = _status_of(found)
            if status == DUPLICATE:
                current = found
                continue
            return nxt if status == OPEN else None
        return None

    def _comment(self, ticket: str, request: ModelBaselineClusterReportRequest) -> None:
        commented = self._call(
            "comment",
            self._store.comment,
            ModelClusterTicketCommentRequest(
                ticket=ticket, body=occurrence_comment(request)
            ),
            request,
        )
        if commented.state == "failed":
            raise _unavailable(f"comment-failed:{commented.cause}")
        if commented.comment_id is None:
            raise _unavailable("comment-failed:no-comment-id")

    def _create(
        self,
        carrying: list[tuple[str, ModelClusterTicketLookupResult]],
        request: ModelBaselineClusterReportRequest,
    ) -> ModelBaselineClusterReportResult:
        previous = next(
            (
                ticket
                for ticket, found in carrying
                if _status_of(found) in CLOSED_STATUSES
            ),
            None,
        )
        created = self._call(
            "create",
            self._store.create,
            ModelClusterTicketCreateRequest(
                title=TITLE_TEMPLATE.format(cluster=request.cluster, repo=request.repo),
                description=ticket_description(request, previous),
                parent=request.parent,
            ),
            request,
        )
        if created.state == "refused":
            return ModelBaselineClusterReportResult(
                state="refused", cause=f"{REFUSED_PREFIX}{created.cause}"
            )
        if created.state == "failed" or created.ticket is None:
            raise _unavailable(f"create-failed:{created.cause}")
        return ModelBaselineClusterReportResult(
            state="created", ticket=created.ticket, previous_ticket=previous
        )
