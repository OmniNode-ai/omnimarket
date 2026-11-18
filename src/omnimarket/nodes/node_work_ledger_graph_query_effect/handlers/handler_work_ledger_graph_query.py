# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Answer structural and similarity queries over the work ledger.

The graph comes inline or from a JSON file (the effect: the file read). A
graph that cannot be read, does not validate, names another schema version,
fails its content hash or holds no rows answers status=error, never an empty
answer. Structural queries always report zero model tokens.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path

import httpx
from pydantic import ValidationError

from omnimarket.models.work_ledger_graph.model_work_ledger_graph import (
    WORK_LEDGER_GRAPH_SCHEMA_VERSION,
    EnumLedgerEdgeKind,
    EnumWorkLedgerGraphQuery,
    EnumWorkLedgerGraphQueryStatus,
    ModelLedgerGraphCell,
    ModelLedgerGraphRowNode,
    ModelWorkLedgerGraph,
    ModelWorkLedgerGraphAnswer,
    ModelWorkLedgerGraphQueryRequest,
    ModelWorkLedgerGraphQueryResult,
)
from omnimarket.nodes.node_work_ledger_graph_query_effect.handlers.work_ledger_similarity import (
    WorkLedgerSimilarity,
)
from omnimarket.nodes.node_work_ledger_graph_query_effect.handlers.work_ledger_similarity_projection import (
    LedgerSimilarityPageReader,
)

__all__ = [
    "HandlerWorkLedgerGraphQuery",
    "LedgerGraphIndex",
    "answer_query",
    "load_graph",
]

LANDING_CONTROLLER = "lane:landing-controller"
_PR_CLOSED_KEYS = ("outcome", "result", "state", "step")
_PR_CLOSED_VALUES = frozenset({"merged", "closed", "landed"})
_TICKET_CLOSED_KEYS = ("outcome", "state", "linear", "ticket_state")
_TICKET_CLOSED_VALUES = frozenset({"done", "canceled", "cancelled", "duplicate"})
_REVIEW_VERDICTS = frozenset(
    {
        "CHANGES_NEEDED",
        "APPROVE",
        "APPROVED",
        "LGTM",
        "NO_FINDINGS",
        "CLEAN",
        "BLOCKING",
    }
)
_MAX_ROW_IDS = 10


class GraphUnreadableError(ValueError):
    """The graph cannot answer: unreadable, invalid, wrong version, bad hash or empty."""


def _canonical_sha(graph: ModelWorkLedgerGraph) -> str:
    unhashed = graph.model_copy(update={"content_sha256": ""})
    canonical = json.dumps(
        unhashed.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_graph(request: ModelWorkLedgerGraphQueryRequest) -> ModelWorkLedgerGraph:
    if request.graph is not None:
        graph = request.graph
    elif request.graph_path:
        try:
            text = Path(request.graph_path).read_text(encoding="utf-8")
        except OSError as exc:
            raise GraphUnreadableError(f"graph file unreadable: {exc}") from exc
        try:
            graph = ModelWorkLedgerGraph.model_validate_json(text)
        except ValidationError as exc:
            raise GraphUnreadableError(
                f"graph file does not validate: {exc.error_count()} errors"
            ) from exc
    else:
        raise GraphUnreadableError("request names no graph and no graph_path")
    if graph.schema_version != WORK_LEDGER_GRAPH_SCHEMA_VERSION:
        raise GraphUnreadableError(
            f"graph schema {graph.schema_version!r} is not {WORK_LEDGER_GRAPH_SCHEMA_VERSION!r}"
        )
    if graph.row_count == 0 or not graph.rows:
        raise GraphUnreadableError("graph holds no rows")
    if graph.row_count != len(graph.rows):
        raise GraphUnreadableError(
            f"graph row_count {graph.row_count} != {len(graph.rows)} rows"
        )
    if graph.content_sha256 != _canonical_sha(graph):
        raise GraphUnreadableError("graph content_sha256 does not match its content")
    return graph


def _cell(row: ModelLedgerGraphRowNode, key: str) -> str | None:
    for cell in row.cells:
        if cell.key == key:
            return cell.value
    return None


def _head(value: str) -> str:
    return value.strip().lower()[:12]


class LedgerGraphIndex:
    """Lookups over one graph: rows by id, targets by row and rows by target."""

    def __init__(self, graph: ModelWorkLedgerGraph) -> None:
        self.rows: dict[str, ModelLedgerGraphRowNode] = {
            r.row_id: r for r in graph.rows
        }
        self.order: dict[str, int] = {r.row_id: i for i, r in enumerate(graph.rows)}
        self.by_row: dict[str, list[tuple[EnumLedgerEdgeKind, str]]] = defaultdict(list)
        self.by_target: dict[str, list[str]] = defaultdict(list)
        for edge in graph.edges:
            self.by_row[edge.row_id].append((edge.kind, edge.target))
            self.by_target[edge.target].append(edge.row_id)
        for target, row_ids in self.by_target.items():
            self.by_target[target] = sorted(set(row_ids), key=self.order.__getitem__)

    def targets(self, row_id: str, kind: EnumLedgerEdgeKind) -> list[str]:
        return [t for k, t in self.by_row.get(row_id, []) if k is kind]

    def all_targets(self, prefix: str) -> list[str]:
        return sorted(t for t in self.by_target if t.startswith(prefix))

    def live_claim_lanes(self) -> set[str]:
        opened = {
            t
            for row_id in self.rows
            for t in self.targets(row_id, EnumLedgerEdgeKind.OPENS_CLAIM)
        }
        closed = {
            t
            for row_id in self.rows
            for t in self.targets(row_id, EnumLedgerEdgeKind.CLOSES_CLAIM)
        }
        return {
            "lane:" + claim[len("claim:") :].rsplit("@", 1)[0]
            for claim in opened - closed
        }

    def pr_closed(self, pr: str) -> bool:
        for row_id in self.by_target.get(pr, []):
            row = self.rows[row_id]
            for key in _PR_CLOSED_KEYS:
                value = _cell(row, key)
                if value is not None and value.lower() in _PR_CLOSED_VALUES:
                    return True
        return False

    def ticket_closed(self, ticket: str) -> bool:
        for row_id in self.by_target.get(ticket, []):
            row = self.rows[row_id]
            if ticket not in self.targets(row_id, EnumLedgerEdgeKind.NAMES_TICKET):
                continue
            for key in _TICKET_CLOSED_KEYS:
                value = _cell(row, key)
                if value is not None and value.lower() in _TICKET_CLOSED_VALUES:
                    return True
        return False

    def open_prs(self) -> list[str]:
        return [pr for pr in self.all_targets("pr:") if not self.pr_closed(pr)]


def _facts(**values: str | int | None) -> tuple[ModelLedgerGraphCell, ...]:
    return tuple(
        ModelLedgerGraphCell(key=k, value=str(v))
        for k, v in values.items()
        if v is not None
    )


def _open_prs_no_live_owner(ix: LedgerGraphIndex) -> list[ModelWorkLedgerGraphAnswer]:
    live = ix.live_claim_lanes()
    answers = []
    for pr in ix.open_prs():
        row_ids = ix.by_target[pr]
        owned = handed = False
        for row_id in row_ids:
            if set(ix.targets(row_id, EnumLedgerEdgeKind.IN_LANE)) & live:
                owned = True
            to = ix.targets(row_id, EnumLedgerEdgeKind.MSG_TO) + ix.targets(
                row_id, EnumLedgerEdgeKind.HANDED_TO
            )
            if (
                LANDING_CONTROLLER in to
                or _cell(ix.rows[row_id], "handed-off") is not None
            ):
                handed = True
        if owned or handed:
            continue
        last = ix.rows[row_ids[-1]]
        answers.append(
            ModelWorkLedgerGraphAnswer(
                subject=pr,
                facts=_facts(
                    rows=len(row_ids), last_row_ts=last.row_ts, last_lane=last.lane
                ),
                row_ids=tuple(row_ids[-_MAX_ROW_IDS:]),
            )
        )
    return answers


def _heads_reviewed_more_than_once(
    ix: LedgerGraphIndex,
) -> list[ModelWorkLedgerGraphAnswer]:
    reviews: dict[str, list[str]] = defaultdict(list)
    for row_id, row in ix.rows.items():
        verdict = _cell(row, "verdict")
        if verdict is None or verdict.upper().replace("-", "_") not in _REVIEW_VERDICTS:
            continue
        for head in ix.targets(row_id, EnumLedgerEdgeKind.NAMES_HEAD):
            reviews[_head(head[len("head:") :])].append(row_id)
    answers = []
    for head in sorted(reviews):
        row_ids = sorted(set(reviews[head]), key=ix.order.__getitem__)
        if len(row_ids) < 2:
            continue
        prs = sorted(
            {p for r in row_ids for p in ix.targets(r, EnumLedgerEdgeKind.NAMES_PR)}
        )
        verdicts = ",".join(_cell(ix.rows[r], "verdict") or "" for r in row_ids)
        answers.append(
            ModelWorkLedgerGraphAnswer(
                subject=f"head:{head}",
                facts=_facts(
                    reviews=len(row_ids), verdicts=verdicts, prs=",".join(prs) or None
                ),
                row_ids=tuple(row_ids[:_MAX_ROW_IDS]),
            )
        )
    answers.sort(key=lambda a: (-len(a.row_ids), a.subject))
    return answers


def _red_check_holding_most_open_prs(
    ix: LedgerGraphIndex,
) -> list[ModelWorkLedgerGraphAnswer]:
    open_prs = set(ix.open_prs())
    cause_checks: dict[str, set[str]] = defaultdict(set)
    cause_prs: dict[str, set[str]] = defaultdict(set)
    check_prs: dict[str, set[str]] = defaultdict(set)
    check_rows: dict[str, set[str]] = defaultdict(set)
    for row_id in ix.rows:
        checks = ix.targets(row_id, EnumLedgerEdgeKind.NAMES_CHECK)
        causes = ix.targets(row_id, EnumLedgerEdgeKind.NAMES_CAUSE)
        prs = set(ix.targets(row_id, EnumLedgerEdgeKind.NAMES_PR))
        for check in checks:
            check_prs[check] |= prs
            check_rows[check].add(row_id)
            for cause in causes:
                cause_checks[cause].add(check)
        for cause in causes:
            cause_prs[cause] |= prs
    for cause, cause_check_set in cause_checks.items():
        for check in cause_check_set:
            check_prs[check] |= cause_prs[cause]
    answers = []
    for check in sorted(check_prs):
        held = sorted(check_prs[check] & open_prs)
        if not held:
            continue
        rows = sorted(check_rows[check], key=ix.order.__getitem__)
        answers.append(
            ModelWorkLedgerGraphAnswer(
                subject=check,
                facts=_facts(open_prs=len(held), prs=",".join(held)),
                row_ids=tuple(rows[-_MAX_ROW_IDS:]),
            )
        )
    answers.sort(key=lambda a: (-int(a.facts[0].value), a.subject))
    return answers


def _mechanism_built(ix: LedgerGraphIndex, ticket: str) -> bool:
    for row_id in ix.by_target.get(ticket, []):
        if ticket not in ix.targets(row_id, EnumLedgerEdgeKind.NAMES_TICKET):
            continue
        row = ix.rows[row_id]
        if _cell(row, "installed") and _cell(row, "observed"):
            return True
        if row.row_type == "TERMINAL" and (_cell(row, "outcome") or "").lower() in {
            "done",
            "merged",
            "landed",
        }:
            return True
    return False


def _process_rulings_no_mechanism(
    ix: LedgerGraphIndex,
) -> list[ModelWorkLedgerGraphAnswer]:
    answers = []
    for row_id, row in ix.rows.items():
        if row.row_type != "RULING" or (_cell(row, "kind") or "").lower() != "process":
            continue
        mechanisms = ix.targets(row_id, EnumLedgerEdgeKind.MECHANISM_TICKET)
        unbuilt = [m for m in mechanisms if not _mechanism_built(ix, m)]
        if mechanisms and not unbuilt:
            continue
        answers.append(
            ModelWorkLedgerGraphAnswer(
                subject=f"ruling:{row.row_ts}",
                facts=_facts(
                    lane=row.lane,
                    mechanism=",".join(m[len("ticket:") :] for m in unbuilt)
                    if unbuilt
                    else "none-named",
                    reason="mechanism-not-built" if unbuilt else "no-mechanism-ticket",
                ),
                row_ids=(row_id,),
            )
        )
    return answers


def _seen_running_ticket_open(ix: LedgerGraphIndex) -> list[ModelWorkLedgerGraphAnswer]:
    seen: dict[str, list[str]] = defaultdict(list)
    for row_id, row in ix.rows.items():
        if not (_cell(row, "installed") and _cell(row, "observed")):
            continue
        for ticket in ix.targets(row_id, EnumLedgerEdgeKind.NAMES_TICKET):
            seen[ticket].append(row_id)
    answers = []
    for ticket in sorted(seen):
        if ix.ticket_closed(ticket):
            continue
        row_ids = sorted(set(seen[ticket]), key=ix.order.__getitem__)
        last = ix.rows[row_ids[-1]]
        answers.append(
            ModelWorkLedgerGraphAnswer(
                subject=ticket,
                facts=_facts(
                    seen_rows=len(row_ids),
                    installed=_cell(last, "installed"),
                    last_row_ts=last.row_ts,
                    prs=",".join(
                        sorted(
                            {
                                p
                                for r in row_ids
                                for p in ix.targets(r, EnumLedgerEdgeKind.NAMES_PR)
                            }
                        )
                    )
                    or None,
                ),
                row_ids=tuple(row_ids[-_MAX_ROW_IDS:]),
            )
        )
    return answers


_QUERIES: dict[
    EnumWorkLedgerGraphQuery,
    Callable[[LedgerGraphIndex], list[ModelWorkLedgerGraphAnswer]],
] = {
    EnumWorkLedgerGraphQuery.OPEN_PRS_NO_LIVE_OWNER: _open_prs_no_live_owner,
    EnumWorkLedgerGraphQuery.HEADS_REVIEWED_MORE_THAN_ONCE: _heads_reviewed_more_than_once,
    EnumWorkLedgerGraphQuery.RED_CHECK_HOLDING_MOST_OPEN_PRS: _red_check_holding_most_open_prs,
    EnumWorkLedgerGraphQuery.PROCESS_RULINGS_NO_MECHANISM: _process_rulings_no_mechanism,
    EnumWorkLedgerGraphQuery.SEEN_RUNNING_TICKET_OPEN: _seen_running_ticket_open,
}


def answer_query(
    request: ModelWorkLedgerGraphQueryRequest,
) -> ModelWorkLedgerGraphQueryResult:
    started = time.perf_counter()
    try:
        if not isinstance(request.query, EnumWorkLedgerGraphQuery):
            raise GraphUnreadableError("request is not a structural graph query")
        graph = load_graph(request)
    except GraphUnreadableError as exc:
        return ModelWorkLedgerGraphQueryResult(
            query=request.query,
            status=EnumWorkLedgerGraphQueryStatus.ERROR,
            error=str(exc),
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
        )
    answers = _QUERIES[request.query](LedgerGraphIndex(graph))
    return ModelWorkLedgerGraphQueryResult(
        query=request.query,
        status=EnumWorkLedgerGraphQueryStatus.OK,
        graph_sha256=graph.content_sha256,
        answers=tuple(answers),
        elapsed_ms=(time.perf_counter() - started) * 1000.0,
    )


class HandlerWorkLedgerGraphQuery:
    """Handle structural queries and tagged similarity over the ledger."""

    def __init__(
        self,
        http_transport: httpx.BaseTransport | None = None,
        page_reader: LedgerSimilarityPageReader | None = None,
    ) -> None:
        self._similarity = WorkLedgerSimilarity(http_transport, page_reader)

    def handle(
        self, request: ModelWorkLedgerGraphQueryRequest
    ) -> ModelWorkLedgerGraphQueryResult:
        if not isinstance(request.query, EnumWorkLedgerGraphQuery):
            return self._similarity.handle(request)
        return answer_query(request)

    async def handle_async(
        self, request: ModelWorkLedgerGraphQueryRequest
    ) -> ModelWorkLedgerGraphQueryResult:
        """Keep network and index work off the runtime's event loop."""
        if isinstance(request.query, EnumWorkLedgerGraphQuery):
            return answer_query(request)
        return await asyncio.to_thread(self.handle, request)
