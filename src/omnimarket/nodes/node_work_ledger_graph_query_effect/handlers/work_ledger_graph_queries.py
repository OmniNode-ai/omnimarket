# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The five typed questions, each answered from the derived graph alone.

No query reads a row's free text: every answer is a walk over nodes and edges the
compute derivation built from the rows' own cells.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from omnimarket.models.work_ledger_graph import (
    EnumWorkLedgerGraphQuery,
    ModelWorkLedgerGraph,
    ModelWorkLedgerGraphAnswerItem,
    ModelWorkLedgerGraphQueryRequest,
)

# A row cell that says the PR is no longer open.
_PR_DONE_OUTCOMES = {"merged", "skipped-merged", "closed", "skipped-closed"}
# verdict= values a PR review writes; probe verdicts (WORKING, NOT_WORKING, UNKNOWN) are not reviews.
_REVIEW_VERDICTS = {
    "CHANGES-NEEDED",
    "CHANGES",
    "CLEAN",
    "APPROVE",
    "APPROVED",
    "LGTM",
    "BLOCKING",
    "REQUEST-CHANGES",
}
_LANDING = "landing-controller"

LEDGER_OPEN_PR_RULE = (
    "ledger: a PR named in the window with no row carrying merged=, outcome=merged|"
    "skipped-merged|closed or action=merged for it"
)
REQUEST_OPEN_PR_RULE = "request: open_prs supplied by the caller"
LEDGER_TICKET_RULE = (
    "ledger: the ledger carries no ticket state cell, so every cited ticket counts open"
)
REQUEST_TICKET_RULE = "request: closed_tickets supplied by the caller"


class WorkLedgerGraphQueryError(RuntimeError):
    """The graph cannot answer: the caller gets an error answer, never an empty one."""


@dataclass
class GraphIndex:
    rows: dict[str, dict[str, str]] = field(default_factory=dict)
    out: dict[tuple[str, str], list[str]] = field(
        default_factory=lambda: defaultdict(list)
    )
    into: dict[tuple[str, str], list[str]] = field(
        default_factory=lambda: defaultdict(list)
    )
    kinds: dict[str, str] = field(default_factory=dict)

    def targets(self, src: str, rel: str) -> list[str]:
        return self.out.get((src, rel), [])

    def sources(self, dst: str, rel: str) -> list[str]:
        return self.into.get((dst, rel), [])

    def of_kind(self, kind: str) -> list[str]:
        return sorted(n for n, k in self.kinds.items() if k == kind)


def build_index(graph: ModelWorkLedgerGraph) -> GraphIndex:
    if graph.row_count == 0 or not graph.nodes:
        raise WorkLedgerGraphQueryError("graph unreadable: no rows in the window")
    index = GraphIndex()
    for node in graph.nodes:
        index.kinds[node.node_id] = node.kind
        if node.kind == "row":
            index.rows[node.node_id] = dict(node.attrs)
    for edge in graph.edges:
        if edge.src not in index.kinds or edge.dst not in index.kinds:
            raise WorkLedgerGraphQueryError(
                f"graph unreadable: edge {edge.rel} names a missing node"
            )
        index.out[(edge.src, edge.rel)].append(edge.dst)
        index.into[(edge.dst, edge.rel)].append(edge.src)
    return index


def _strip(node_id: str) -> str:
    return node_id.split(":", 1)[1]


def _pr_rows(index: GraphIndex, pr: str) -> list[str]:
    return sorted(
        set(index.sources(pr, "NAMES_PR")) | set(index.sources(pr, "HANDS_OFF")),
        key=lambda r: (index.rows[r].get("ts", ""), r),
    )


def _pr_is_done(index: GraphIndex, pr: str) -> bool:
    for row in _pr_rows(index, pr):
        attrs = index.rows[row]
        if attrs.get("merged", "none").lower() not in {"", "none", "no", "false"}:
            return True
        if attrs.get("outcome", "").lower() in _PR_DONE_OUTCOMES:
            return True
        if attrs.get("action", "").lower() == "merged":
            return True
    return False


def open_prs(
    index: GraphIndex, request: ModelWorkLedgerGraphQueryRequest
) -> tuple[list[str], str]:
    every = index.of_kind("pr")
    if request.open_prs is not None:
        wanted = {f"pr:{p.split('/')[-1]}" for p in request.open_prs}
        return [p for p in every if p in wanted], REQUEST_OPEN_PR_RULE
    return [p for p in every if not _pr_is_done(index, p)], LEDGER_OPEN_PR_RULE


def _live_claims(index: GraphIndex) -> dict[str, list[str]]:
    """lane node -> its CLAIM rows that no TERMINAL closed."""
    terminals: dict[str, list[str]] = defaultdict(list)
    for attrs in index.rows.values():
        if attrs.get("type") == "TERMINAL" and attrs.get("lane"):
            terminals[f"lane:{attrs['lane']}"].append(attrs.get("ts", ""))
    live: dict[str, list[str]] = defaultdict(list)
    for row, attrs in index.rows.items():
        if attrs.get("type") != "CLAIM" or not attrs.get("lane"):
            continue
        if index.sources(row, "CLOSES"):
            continue
        lane = f"lane:{attrs['lane']}"
        # A TERMINAL of the same lane at or after the claim closes it when it names no claim.
        if any(ts >= attrs.get("ts", "") for ts in terminals.get(lane, [])):
            continue
        live[lane].append(row)
    return live


def _handed_off(index: GraphIndex, pr: str) -> bool:
    if index.sources(pr, "HANDS_OFF"):
        return True
    for row in index.sources(pr, "NAMES_PR"):
        attrs = index.rows[row]
        if attrs.get("type") == "MSG" and attrs.get("to") == _LANDING:
            return True
        if attrs.get("handed_to") == _LANDING:
            return True
    return False


def q_open_prs_without_owner(
    index: GraphIndex, request: ModelWorkLedgerGraphQueryRequest
) -> tuple[list[ModelWorkLedgerGraphAnswerItem], str]:
    live = _live_claims(index)
    prs, rule = open_prs(index, request)
    items = []
    for pr in prs:
        rows = _pr_rows(index, pr)
        lanes = sorted(
            {index.rows[r]["lane"] for r in rows if index.rows[r].get("lane")}
        )
        owners = [lane for lane in lanes if live.get(f"lane:{lane}")]
        if owners or _handed_off(index, pr):
            continue
        last = rows[-1] if rows else ""
        items.append(
            ModelWorkLedgerGraphAnswerItem(
                subject=_strip(pr),
                count=len(rows),
                prs=(_strip(pr),),
                lanes=tuple(lanes),
                row_ids=tuple(_strip(r) for r in rows[-5:]),
                detail=f"last named {index.rows[last].get('ts', '')} by "
                f"{index.rows[last].get('lane', '')}; no live CLAIM lane names it; not handed off"
                if last
                else "no row",
            )
        )
    return items, rule


def q_heads_reviewed_more_than_once(
    index: GraphIndex, request: ModelWorkLedgerGraphQueryRequest
) -> tuple[list[ModelWorkLedgerGraphAnswerItem], str]:
    items = []
    for head in index.of_kind("head"):
        reviews = sorted(
            (
                r
                for r in index.sources(head, "NAMES_HEAD")
                if index.rows[r].get("verdict", "").upper().replace("_", "-")
                in _REVIEW_VERDICTS
            ),
            key=lambda r: (index.rows[r].get("ts", ""), r),
        )
        if len(reviews) < 2:
            continue
        prs = sorted({_strip(p) for r in reviews for p in index.targets(r, "NAMES_PR")})
        items.append(
            ModelWorkLedgerGraphAnswerItem(
                subject=_strip(head),
                count=len(reviews),
                prs=tuple(prs),
                lanes=tuple(
                    sorted({index.rows[r].get("lane", "") for r in reviews} - {""})
                ),
                row_ids=tuple(_strip(r) for r in reviews),
                detail="verdicts: "
                + ", ".join(index.rows[r].get("verdict", "") for r in reviews),
            )
        )
    items.sort(key=lambda i: (-i.count, i.subject))
    return items, "review rows: verdict= in the review set beside head="


def q_red_check_holding_most_prs(
    index: GraphIndex, request: ModelWorkLedgerGraphQueryRequest
) -> tuple[list[ModelWorkLedgerGraphAnswerItem], str]:
    prs, rule = open_prs(index, request)
    open_set = set(prs)
    items = []
    for check in index.of_kind("check"):
        held = sorted({p for p in index.targets(check, "HOLDS_PR") if p in open_set})
        if not held:
            continue
        rows = sorted(set(index.sources(check, "NAMES_CHECK")))
        items.append(
            ModelWorkLedgerGraphAnswerItem(
                subject=_strip(check),
                count=len(held),
                prs=tuple(_strip(p) for p in held),
                row_ids=tuple(_strip(r) for r in rows[:20]),
                detail=f"{len(held)} open PRs named beside check= on {len(rows)} rows",
            )
        )
    items.sort(key=lambda i: (-i.count, i.subject))
    return items, rule


def _built_and_wired(index: GraphIndex, ticket: str) -> bool:
    for row in index.sources(ticket, "CITES"):
        attrs = index.rows[row]
        if attrs.get("installed") and attrs.get("observed") == "yes":
            return True
    return False


def q_process_rulings_without_mechanism(
    index: GraphIndex, request: ModelWorkLedgerGraphQueryRequest
) -> tuple[list[ModelWorkLedgerGraphAnswerItem], str]:
    items = []
    rulings = sorted(
        (
            r
            for r, a in index.rows.items()
            if a.get("type") == "RULING" and a.get("kind") == "process"
        ),
        key=lambda r: (index.rows[r].get("ts", ""), r),
    )
    for ruling in rulings:
        mechanisms = sorted(index.targets(ruling, "MECHANISM"))
        if any(_built_and_wired(index, m) for m in mechanisms):
            continue
        attrs = index.rows[ruling]
        items.append(
            ModelWorkLedgerGraphAnswerItem(
                subject=attrs.get("ts", _strip(ruling)),
                tickets=tuple(_strip(m) for m in mechanisms),
                lanes=(attrs.get("lane", ""),) if attrs.get("lane") else (),
                row_ids=(_strip(ruling),),
                detail="no mechanism-ticket= cell"
                if not mechanisms
                else "no row citing the mechanism ticket carries installed= and observed=",
            )
        )
    return (
        items,
        "built and wired: a row citing the mechanism ticket carries installed= and observed=",
    )


def q_seen_running_with_open_tickets(
    index: GraphIndex, request: ModelWorkLedgerGraphQueryRequest
) -> tuple[list[ModelWorkLedgerGraphAnswerItem], str]:
    closed = set(request.closed_tickets or ())
    rule = (
        REQUEST_TICKET_RULE
        if request.closed_tickets is not None
        else LEDGER_TICKET_RULE
    )
    by_change: dict[str, list[str]] = defaultdict(list)
    for row, attrs in index.rows.items():
        if not (attrs.get("installed") and attrs.get("observed") == "yes"):
            continue
        for pr in index.targets(row, "NAMES_PR"):
            by_change[pr].append(row)
    items = []
    for pr in sorted(by_change):
        rows = sorted(by_change[pr], key=lambda r: (index.rows[r].get("ts", ""), r))
        tickets = sorted(
            {_strip(t) for r in rows for t in index.targets(r, "CITES")} - closed
        )
        if not tickets:
            continue
        last = index.rows[rows[-1]]
        items.append(
            ModelWorkLedgerGraphAnswerItem(
                subject=_strip(pr),
                count=len(rows),
                prs=(_strip(pr),),
                tickets=tuple(tickets),
                lanes=tuple(
                    sorted({index.rows[r].get("lane", "") for r in rows} - {""})
                ),
                row_ids=tuple(_strip(r) for r in rows[-5:]),
                detail=f"installed={last.get('installed', '')} observed at {last.get('ts', '')}",
            )
        )
    return items, rule


QUERIES = {
    EnumWorkLedgerGraphQuery.OPEN_PRS_WITHOUT_OWNER: q_open_prs_without_owner,
    EnumWorkLedgerGraphQuery.HEADS_REVIEWED_MORE_THAN_ONCE: q_heads_reviewed_more_than_once,
    EnumWorkLedgerGraphQuery.RED_CHECK_HOLDING_MOST_PRS: q_red_check_holding_most_prs,
    EnumWorkLedgerGraphQuery.PROCESS_RULINGS_WITHOUT_MECHANISM: q_process_rulings_without_mechanism,
    EnumWorkLedgerGraphQuery.SEEN_RUNNING_WITH_OPEN_TICKETS: q_seen_running_with_open_tickets,
}


def answer(
    graph: ModelWorkLedgerGraph, request: ModelWorkLedgerGraphQueryRequest
) -> tuple[list[ModelWorkLedgerGraphAnswerItem], str]:
    return QUERIES[request.query](build_index(graph), request)


__all__ = [
    "QUERIES",
    "GraphIndex",
    "WorkLedgerGraphQueryError",
    "answer",
    "build_index",
]
