# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Enumerate the approved-work list: validate each row, then judge it on the facts (OMN-23250).

Moved from the frozen lab-fill script's enumerate, row validation and skip order unchanged. The
fact reads (watcher, merged history, Linear) each take their own floor of the budget, so one slow
read is named and the others are not blamed.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime

from ..models.model_lab_fill_approved_work import (
    APPROVED_STAGES,
    ModelLabFillApprovedCandidate,
    ModelLabFillApprovedWorkRequest,
    ModelLabFillApprovedWorkResult,
)
from ..protocols import LabFillPortError, LabFillSourceCutError, ProtocolLabFillClock
from ..protocols.protocol_lab_fill_enumerate_facts import (
    LabFillApprovedTicket,
    LabFillMergedHistory,
    LabFillPrFacts,
    ProtocolLabFillAcceptanceProbe,
    ProtocolLabFillApprovedPrFacts,
    ProtocolLabFillApprovedRows,
    ProtocolLabFillApprovedTickets,
    ProtocolLabFillMergedHistory,
)
from .helpers_lab_fill_effect import default_clock
from .helpers_lab_fill_enumerate import acceptance_references, declared_repository

_REQUIRED = (
    "id",
    "kind",
    "ticket",
    "repo",
    "goal",
    "acceptance_check",
    "added_by",
    "added_at",
)
_TICKET = re.compile(r"OMN-[0-9]+")
_NAME = re.compile(r"[A-Za-z0-9_.-]+")
_STAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:]{8}Z")
DONE_STATES = ("completed", "canceled")


def _text(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and "\n" not in value
        and "\r" not in value
    )


def _stamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def row_is_valid(
    row: object, ids: set[str], kinds: Sequence[str], done_bases: Sequence[str]
) -> bool:
    """The old script's row rules: required text cells, shapes of the ids, a unique id, and the optional cells."""
    if not isinstance(row, dict) or any(not _text(row.get(k)) for k in _REQUIRED):
        return False
    if row["kind"] not in kinds or not _TICKET.fullmatch(row["ticket"]):
        return False
    if (
        not _NAME.fullmatch(row["id"])
        or not _NAME.fullmatch(row["repo"])
        or row["id"] in ids
    ):
        return False
    if not _STAMP.fullmatch(row["added_at"]):
        return False
    for cell in ("blocked_until", "repo_confirmed_by", "remaining"):
        if cell in row and not _text(row[cell]):
            return False
    if "blocked_on" in row:
        blocked = row["blocked_on"]
        if (
            not isinstance(blocked, list)
            or not blocked
            or any(not isinstance(t, str) or not _TICKET.fullmatch(t) for t in blocked)
            or len(set(blocked)) != len(blocked)
            or row["ticket"] in blocked
        ):
            return False
    if "host" in row and not (
        isinstance(row["host"], str) and _NAME.fullmatch(row["host"])
    ):
        return False
    if "done" in row and not isinstance(row["done"], bool):
        return False
    if row.get("done") is True and not _text(row.get("done_evidence")):
        return False
    if "done_basis" in row and row["done_basis"] not in done_bases:
        return False
    if "checked_at" in row and not (
        isinstance(row["checked_at"], str) and _STAMP.fullmatch(row["checked_at"])
    ):
        return False
    try:
        _stamp(row["added_at"])
        _stamp(str(row.get("checked_at", row["added_at"])))
    except ValueError:
        return False
    return True


def skip_reason(
    row: Mapping[str, object],
    node: LabFillApprovedTicket | None,
    prs: LabFillPrFacts,
    history: LabFillMergedHistory,
) -> str:
    """Why a row is not dispatched, in the old script's order; empty when it stands."""
    ticket = str(row["ticket"])
    if ticket in prs.merged_tickets:
        return "done:pr-merged"
    if node is not None and node.state_type in DONE_STATES:
        return "done:ticket"
    if row.get("done") is True:
        return "done:declared"
    if node is None:
        return "unreadable:ticket"
    repo = str(row["repo"])
    if repo in history.unread_repos:
        return f"unreadable:merged-history:{repo}"
    if ticket in history.newest and (
        not row.get("checked_at")
        or _stamp(history.newest[ticket][1]) > _stamp(str(row["checked_at"]))
    ):
        name, _, sha = history.newest[ticket]
        return f"stale:merged-after-check:{name}@{sha[:12]}"
    if row.get("blocked_until"):
        return f"blocked-until:{row['blocked_until']}"
    if ticket in prs.open_tickets:
        return "owned:open-pr:" + ",".join(sorted(prs.open_tickets[ticket]))
    return ""


def _blocked_on(row: Mapping[str, object]) -> list[str]:
    blocked = row.get("blocked_on")
    return [str(b) for b in blocked] if isinstance(blocked, list) else []


def _detail(exc: Exception) -> str:
    return " ".join(str(exc).split())[:100]


class HandlerLabFillApprovedWork:
    """Read the approved-work file and judge each valid row on the watcher, the clones' history and Linear."""

    def __init__(
        self,
        rows: ProtocolLabFillApprovedRows | None = None,
        prs: ProtocolLabFillApprovedPrFacts | None = None,
        history: ProtocolLabFillMergedHistory | None = None,
        tickets: ProtocolLabFillApprovedTickets | None = None,
        acceptance: ProtocolLabFillAcceptanceProbe | None = None,
        clock: ProtocolLabFillClock | None = None,
    ) -> None:
        # Injectable parameters only, so the runtime can build this at boot; the operation
        # refuses by name on a host that has not injected the readers.
        self._ports = (rows, prs, history, tickets, acceptance)
        self._clock = default_clock(clock)

    def _need(
        self,
    ) -> tuple[
        ProtocolLabFillApprovedRows,
        ProtocolLabFillApprovedPrFacts,
        ProtocolLabFillMergedHistory,
        ProtocolLabFillApprovedTickets,
        ProtocolLabFillAcceptanceProbe,
    ]:
        rows, prs, history, tickets, acceptance = self._ports
        if None in (rows, prs, history, tickets, acceptance):
            raise LabFillPortError(
                "enumerate_lab_fill_approved_work: a reader is not injected on this host"
            )
        assert rows is not None
        assert prs is not None
        assert history is not None
        assert tickets is not None
        assert acceptance is not None
        return rows, prs, history, tickets, acceptance

    def handle(
        self, request: ModelLabFillApprovedWorkRequest
    ) -> ModelLabFillApprovedWorkResult:
        rows_port, prs_port, history_port, tickets_port, acceptance = self._need()
        start = self._clock.now_epoch_s()
        seen: Counter[str] = Counter()
        invalid: Counter[str] = Counter()

        def done(state: str, **kw: object) -> ModelLabFillApprovedWorkResult:
            return ModelLabFillApprovedWorkResult.model_validate(
                {
                    "state": state,
                    "seen": dict(seen),
                    "invalid": dict(invalid),
                    "elapsed_s": self._clock.now_epoch_s() - start,
                    **kw,
                }
            )

        try:
            declared = rows_port(request.path)
        except LabFillPortError as exc:
            return done("unreadable", failed_stage="approved-rows", detail=_detail(exc))
        valid: list[dict[str, object]] = []
        ids: set[str] = set()
        for row in declared:
            kind = row.get("kind") if isinstance(row, dict) else None
            kind = kind if kind in request.kinds else request.kinds[0]
            seen[str(kind)] += 1
            if not row_is_valid(row, ids, request.kinds, request.done_bases):
                invalid[str(kind)] += 1
                continue
            assert isinstance(row, dict)
            ids.add(str(row["id"]))
            valid.append(row)
        if not valid:
            return done("empty")
        if len(declared) > request.max_rows:
            return done(
                "unreadable",
                failed_stage="approved-rows",
                detail=f"approved-list exceeds {request.max_rows} rows",
            )
        wanted = sorted(
            {t for row in valid for t in [str(row["ticket"]), *_blocked_on(row)]},
            key=lambda t: int(t.split("-")[1]),
        )
        row_tickets = sorted({str(row["ticket"]) for row in valid})

        def allowed(stage: str) -> float:
            behind = len(APPROVED_STAGES) - APPROVED_STAGES.index(stage) - 1
            left = request.budget_s - (self._clock.now_epoch_s() - start)
            return max(left - request.stage_floor_s * behind, request.stage_floor_s)

        stage = "pr-facts"
        try:
            prs = prs_port()
            stage = "merged-history"
            history = history_port(row_tickets, allowed(stage))
            stage = "approved-states"
            nodes = tickets_port(wanted, allowed(stage))
        except LabFillSourceCutError as exc:
            return done("stale", failed_stage=stage, detail=_detail(exc))
        except LabFillPortError as exc:
            return done("unreadable", failed_stage=stage, detail=_detail(exc))
        fleet = set(request.fleet_repos)
        candidates: list[ModelLabFillApprovedCandidate] = []
        for row in valid:
            node = nodes.get(str(row["ticket"]))
            blockers = node.blockers if node else ()
            candidates.append(
                ModelLabFillApprovedCandidate(
                    row=row,
                    row_hash=hashlib.sha256(
                        json.dumps(row, sort_keys=True).encode()
                    ).hexdigest(),
                    title=str(row["goal"]),
                    approved_skip=skip_reason(row, node, prs, history),
                    assignee_id=node.assignee_id if node else "",
                    ticket_updated_at=node.updated_at if node else "",
                    ticket_text=node.description if node else "",
                    ticket_blockers=tuple(b for b, _ in blockers),
                    ticket_blockers_done=tuple(
                        b for b, s in blockers if s == "completed"
                    ),
                    acceptance_verified=tuple(
                        ref
                        for ref in acceptance_references(
                            str(row.get("acceptance_check", ""))
                        )
                        if acceptance(ref, str(row["repo"]))
                    ),
                    ticket_prs=node.prs if node else (),
                    merged_implementations=node.merged_implementations if node else (),
                    declared_repo=declared_repository(node.description, fleet)
                    if node
                    else "",
                    prerequisites_done=tuple(
                        t
                        for t in _blocked_on(row)
                        if (nodes.get(t) and nodes[t].state_type == "completed")
                    ),
                )
            )
        return done("ok", candidates=tuple(candidates))
